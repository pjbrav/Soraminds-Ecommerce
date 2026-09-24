"""Pipeline orchestration (spec §5): the 8 steps from upload to publish.

    Upload -> Column Mapping -> Field Validation -> Auto-Repair ->
    Structured Output (+ RAG index) -> Owner Validation Report ->
    Live Preview -> Publish -> Downstream Jobs

Steps 1-5 run synchronously inside process_upload(); steps 6-8 are the API
surface in routers/menu.py.
"""
from __future__ import annotations

import uuid

from . import db, storage
from .audit import audit, utcnow
from .errors import BadFileError, PublishBlockedError, RowNotFoundError, UploadNotFoundError
from .ingest import embeddings
from .ingest.autorepair import apply_defaults_and_sort, auto_repair_row, normalize_price
from .ingest.llm import llm_service, vision_service
from .ingest.mapping import build_mapping
from .ingest.rules import RuleContext, row_status, validate_menu_level, validate_row
from .ingest.sheet import read_sheet
from .tenant_config import TenantConfig
from .records import build_canonical_record


def new_upload_id() -> str:
    return uuid.uuid4().hex[:12]


# --------------------------------------------------------------------------
# Steps 1-5: full ingestion of one upload
# --------------------------------------------------------------------------

def process_upload(
    tenant: TenantConfig,
    upload_id: str,
    workbook_bytes: bytes,
    filename: str,
) -> dict:
    """Run Steps 1-5 for an already-stored upload (files saved by the router).

    Returns a summary dict with counts. Raises on structural problems
    (unparseable file, missing columns, row-count over limit).
    """
    rows = read_sheet(workbook_bytes, filename)
    max_rows = tenant.limit("max_rows", 500)
    if len(rows) > max_rows:
        raise BadFileError(
            f"The file has {len(rows)} item rows; the limit is {max_rows}. "
            "Split the menu across uploads."
        )

    headers = list(rows[0].keys()) if rows else []
    mapping = build_mapping(headers, tenant, embeddings.similarity)
    if mapping.issues:
        has_missing = any(i["code"] == "missing_required_column" for i in mapping.issues)
        if has_missing:
            db.execute("UPDATE uploads SET status='failed' WHERE id=?", (upload_id,))
            raise BadFileError(
                "Required columns missing: "
                + ", ".join(i["field"] for i in mapping.issues if i.get("field"))
            )

    # ---- Auto-repair (Step 4) over every row ------------------------------
    items: list[dict] = []
    for source_row, row in enumerate(rows, start=1):
        mapped = {}
        for header, value in row.items():
            column = mapping.mapping.get(header)
            if column:
                mapped[column] = value
        item = auto_repair_row(mapped, tenant)
        item["source_row"] = source_row
        # Flag unparseable sort_order so validation can raise a soft error.
        sort_raw = mapped.get("sort_order")
        item["sort_order_invalid"] = bool(
            sort_raw is not None
            and str(sort_raw).strip() != ""
            and item["sort_order"] is None
        )
        item["sort_order_raw"] = None if sort_raw is None else str(sort_raw)
        items.append(item)

    apply_defaults_and_sort(items, tenant)
    _assign_item_ids(items)

    # ---- LLM description generation (blank descriptions) -----------------
    for item in items:
        if not item.get("description"):
            desc, method = llm_service.generate_description(item, tenant)
            item["description"] = desc
            item["description_auto_generated"] = True
            item["repairs"].append(f"description: blank -> auto-generated ({method})")

    # ---- Persist items (batched: one transaction) -------------------------
    db.executemany(
        "INSERT INTO upload_items (upload_id, tenant_id, row_index, item_id,"
        " fields_json, status, updated_at) VALUES (?,?,?,?,?,?,?)",
        [
            (
                upload_id,
                tenant.tenant_id,
                idx,
                item["item_id"],
                db.j(_clean_item(item)),
                "valid",
                utcnow(),
            )
            for idx, item in enumerate(items, start=1)
        ],
    )

    # ---- Persist column mapping for audit --------------------------------
    db.execute(
        "UPDATE uploads SET mapping_json=?, status='ready' WHERE id=?",
        (db.j({"mapping": mapping.mapping, "auto_mapped": mapping.auto_mapped}), upload_id),
    )

    # ---- Vision pass: OCR photos, detect conflicts -------------------------
    vision_provider = _run_vision_pass(tenant, upload_id)

    summary = revalidate(tenant, upload_id)
    summary["column_issues"] = mapping.issues
    summary["auto_mapped_columns"] = mapping.auto_mapped
    summary["vision_provider"] = vision_provider
    audit(
        tenant.tenant_id,
        "owner",
        "upload.processed",
        upload_id=upload_id,
        filename=filename,
        rows=len(items),
        vision_provider=vision_provider,
    )
    return summary


_KNOWN_FIELDS = {
    "item_id", "category", "subcategory", "item_name", "description", "price",
    "dietary_type", "spice_level", "calorie_range", "allergens", "image_filename",
    "is_featured", "is_available", "sort_order", "tags",
}




def _assign_item_ids(items: list[dict]) -> None:
    """Blank item_id -> MENU-0001 style, sequential by row order; keep owner IDs."""
    used: set[str] = set()
    counter = 0
    for item in items:
        if item.get("item_id"):
            used.add(item["item_id"])
    for item in items:
        if not item.get("item_id"):
            counter += 1
            candidate = f"MENU-{counter:04d}"
            while candidate in used:
                counter += 1
                candidate = f"MENU-{counter:04d}"
            item["item_id"] = candidate
            item["repairs"].append(f"item_id: blank -> auto-generated '{candidate}'")
            used.add(candidate)


def _clean_item(item: dict) -> dict:
    """The stored subset of an item dict (drops raw helper keys)."""
    return {
        "item_id": item.get("item_id"),
        "category": item.get("category"),
        "subcategory": item.get("subcategory") or "",
        "item_name": item.get("item_name"),
        "description": item.get("description"),
        "description_auto_generated": bool(item.get("description_auto_generated")),
        "price": item.get("price"),
        "dietary_type": item.get("dietary_type"),
        "spice_level": item.get("spice_level"),
        "calorie_range": item.get("calorie_range"),
        "allergens": item.get("allergens") or [],
        "image_filename": item.get("image_filename"),
        "is_featured": bool(item.get("is_featured")),
        "is_available": bool(item.get("is_available")),
        "sort_order": item.get("sort_order"),
        "tags": item.get("tags") or [],
        "repairs": item.get("repairs") or [],
        "source_row": item.get("source_row"),
        "sort_order_invalid": bool(item.get("sort_order_invalid")),
        "conflict_dismissed": bool(item.get("conflict_dismissed")),
        "owner_added": bool(item.get("owner_added")),
        "photo_price": item.get("photo_price"),
    }


def _load_items(upload_id: str) -> list[tuple[int, dict]]:
    rows = db.query(
        "SELECT row_index, fields_json FROM upload_items WHERE upload_id=? ORDER BY row_index",
        (upload_id,),
    )
    return [(r["row_index"], db.unj(r["fields_json"])) for r in rows]


def _photo_map(upload_id: str) -> dict[str, str]:
    """lowercase filename -> actual uploaded filename."""
    photos = db.query("SELECT filename FROM photos WHERE upload_id=?", (upload_id,))
    return {p["filename"].lower(): p["filename"] for p in photos}


def _run_vision_pass(tenant: TenantConfig, upload_id: str) -> str | None:
    """OCR each uploaded photo; compare against the spreadsheet row (Step 7 of claude)."""
    provider = vision_service.available()
    if provider is None:
        db.execute(
            "UPDATE uploads SET notes=COALESCE(notes,'')||? WHERE id=?",
            ("Vision pass skipped: no OPENAI_API_KEY and no local OCR installed. ", upload_id),
        )
        return None
    items_by_image: dict[str, dict] = {}
    for _, item in _load_items(upload_id):
        if item.get("image_filename"):
            items_by_image[item["image_filename"].lower()] = item
    for photo in db.query("SELECT * FROM photos WHERE upload_id=?", (upload_id,)):
        filename = photo["filename"]
        item = items_by_image.get(filename.lower())
        if item is None:
            continue
        path = storage.storage.photo_path(tenant.tenant_id, upload_id, filename)
        if path is None:
            continue
        extracted = vision_service.extract(path.read_bytes())
        if extracted is None:
            continue
        db.execute(
            "UPDATE photos SET ocr_json=? WHERE upload_id=? AND filename=?",
            (db.j(extracted), upload_id, filename),
        )
        # Store the OCR price on the item for conflict checks on every revalidate.
        db.execute(
            "UPDATE upload_items SET fields_json=? WHERE upload_id=? AND row_index=?",
            (
                db.j({**item, "photo_price": extracted.get("price")}),
                upload_id,
                _row_of(upload_id, item),
            ),
        )
    return provider


def _row_of(upload_id: str, item: dict) -> int:
    row = db.query_one(
        "SELECT row_index FROM upload_items WHERE upload_id=? AND item_id=?",
        (upload_id, item["item_id"]),
    )
    return row["row_index"] if row else 0


# --------------------------------------------------------------------------
# Step 6: validation report (recomputed from current state after every edit)
# --------------------------------------------------------------------------

def revalidate(tenant: TenantConfig, upload_id: str) -> dict:
    """Re-run all rules over the CURRENT stored items and rebuild issues.

    Reuses stored OCR results (no re-running the vision model). Called after
    upload, after every owner edit, and after a partial merge.
    """
    _require_upload(upload_id)
    db.execute("DELETE FROM validation_issues WHERE upload_id=?", (upload_id,))
    items = _load_items(upload_id)
    photos = _photo_map(upload_id)
    # Normalize image filenames to the actual on-disk photo name (case and
    # whitespace) so the media URL points at a real file even when the
    # spreadsheet spells it differently (e.g. GOAT_BIRYANI.JPG vs
    # goat_biryani.jpg — matching is case-insensitive, the URL must not be).
    disk_by_lower = {p.lower(): p for p in photos}
    for _, item in items:
        ref = (item.get("image_filename") or "").strip()
        if ref and ref.lower() in disk_by_lower and disk_by_lower[ref.lower()] != ref:
            item["image_filename"] = disk_by_lower[ref.lower()]
    ctx = RuleContext(tenant=tenant, photo_filenames=photos, similarity=embeddings.similarity)

    counts = {"valid": 0, "warning": 0, "error": 0}
    hard_total = 0
    row_issues: dict[int, list[dict]] = {}
    for row_index, item in items:
        # Light re-repair in case the owner's edit left whitespace/currency noise.
        price, note = normalize_price(item.get("price"))
        if note and price is not None:
            item["price"] = price
            item["repairs"] = item.get("repairs", []) + [f"price: {note}"]
        issues = validate_row(item, ctx)
        # Photo conflict (vision): photo price vs spreadsheet price.
        photo_price = item.get("photo_price")
        if (
            photo_price is not None
            and item.get("price") is not None
            and abs(float(photo_price) - float(item["price"])) > 0.004
            and not item.get("conflict_dismissed")
        ):
            issues.append(
                {
                    "field": "price",
                    "code": "photo_conflict",
                    "severity": "hard",
                    "message": (
                        f"The photo for this item shows ${photo_price:.2f} but the "
                        f"spreadsheet says ${item['price']:.2f}."
                    ),
                    "suggestion": (
                        f"Pick the correct price — the photo value (${photo_price:.2f}) "
                        f"or the spreadsheet value (${item['price']:.2f})."
                    ),
                    "extra": {"photo_price": photo_price, "sheet_price": item["price"]},
                }
            )
        row_issues[row_index] = issues

    # Menu-level warnings (featured count, near-duplicates) attach to rows.
    plain_items = [item for _, item in items]
    for row_index, issue in validate_menu_level(plain_items, tenant):
        row_issues.setdefault(row_index, []).append(issue)

    row_updates: list[tuple] = []
    issue_tuples: list[tuple] = []
    for row_index, item in items:
        issues = row_issues.get(row_index, [])
        status = row_status(issues)
        counts[status] += 1
        hard_total += sum(1 for i in issues if i["severity"] == "hard")
        row_updates.append((status, db.j(_clean_item(item)), upload_id, row_index))
        for issue in issues:
            issue_tuples.append(_issue_tuple(upload_id, row_index, issue))

    # Upload-level issues (e.g. featured-count warning, or None-keyed rows).
    for issue in row_issues.get(None, []):
        issue_tuples.append(_issue_tuple(upload_id, None, issue))

    # Batched writes: one transaction for the whole revalidation pass.
    db.executemany(
        "UPDATE upload_items SET status=?, fields_json=? WHERE upload_id=? AND row_index=?",
        row_updates,
    )
    db.executemany(
        "INSERT INTO validation_issues (upload_id, item_row, field, code, severity,"
        " message, suggestion, extra_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        issue_tuples,
    )

    # Unmatched (orphan) photos: resolvable from the dashboard gallery.
    referenced = {(i.get("image_filename") or "").lower() for _, i in items}
    orphan_tuples = []
    for filename in photos.values():
        if filename.lower() not in referenced:
            orphan_tuples.append(
                _issue_tuple(
                    upload_id,
                    None,
                    {
                        "field": "image_filename",
                        "code": "photo_unmatched",
                        "severity": "soft",
                        "message": f"Photo '{filename}' is not referenced by any menu item.",
                        "suggestion": "Assign it to a menu item below, or ignore it — "
                        "it simply won't be used.",
                        "extra": {"filename": filename},
                    },
                )
            )
    db.executemany(
        "INSERT INTO validation_issues (upload_id, item_row, field, code, severity,"
        " message, suggestion, extra_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        orphan_tuples,
    )

    report = {
        "upload_id": upload_id,
        "total_rows": len(items),
        "counts": counts,
        "hard_errors": hard_total,
        "publishable": hard_total == 0,
    }
    audit(tenant.tenant_id, "system", "validation.recomputed", upload_id=upload_id, **counts)
    return report


def _issue_tuple(upload_id: str, row_index: int | None, issue: dict) -> tuple:
    return (
        upload_id,
        row_index,
        issue.get("field"),
        issue["code"],
        issue["severity"],
        issue["message"],
        issue.get("suggestion"),
        db.j(issue.get("extra")) if issue.get("extra") else None,
        utcnow(),
    )


def _insert_issue(upload_id: str, row_index: int | None, issue: dict) -> None:
    db.execute(
        "INSERT INTO validation_issues (upload_id, item_row, field, code, severity,"
        " message, suggestion, extra_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        _issue_tuple(upload_id, row_index, issue),
    )


def get_report(tenant_id: str, upload_id: str) -> dict:
    """The owner-facing validation report payload (Step 6)."""
    upload = _require_upload(upload_id)
    items = db.query(
        "SELECT * FROM upload_items WHERE upload_id=? ORDER BY row_index", (upload_id,)
    )
    issues = db.query(
        "SELECT * FROM validation_issues WHERE upload_id=? ORDER BY item_row IS NULL,"
        " item_row, id",
        (upload_id,),
    )
    photos = db.query("SELECT * FROM photos WHERE upload_id=?", (upload_id,))
    rows = []
    for it in items:
        fields = db.unj(it["fields_json"])
        row_issues = [dict(i) for i in issues if i["item_row"] == it["row_index"]]
        rows.append(
            {
                "row_index": it["row_index"],
                "item_id": it["item_id"],
                "status": it["status"],
                "fields": fields,
                "issues": [_issue_view(i) for i in row_issues],
            }
        )
    counts = {"valid": 0, "warning": 0, "error": 0}
    for r in rows:
        counts[r["status"]] += 1
    hard = sum(1 for i in issues if i["severity"] == "hard")
    return {
        "upload_id": upload_id,
        "tenant_id": upload["tenant_id"],
        "filename": upload["filename"],
        "status": upload["status"],
        "total_rows": len(rows),
        "mapping": db.unj(upload["mapping_json"]),
        "counts": counts,
        "hard_errors": hard,
        "publishable": hard == 0,
        "rows": rows,
        "upload_issues": [_issue_view(i) for i in issues if i["item_row"] is None],
        "photos": [dict(p, ocr=db.unj(p["ocr_json"])) for p in photos],
    }


def _issue_view(issue) -> dict:
    return {
        "field": issue["field"],
        "code": issue["code"],
        "severity": issue["severity"],
        "message": issue["message"],
        "suggestion": issue["suggestion"],
        "extra": db.unj(issue["extra_json"]),
    }


# --------------------------------------------------------------------------
# Step 6 correction paths: inline edit + partial re-upload
# --------------------------------------------------------------------------

PATCHABLE_FIELDS = {
    "item_name", "category", "subcategory", "description", "price", "dietary_type",
    "spice_level", "calorie_range", "allergens", "image_filename", "is_featured",
    "is_available", "sort_order", "tags",
}


def apply_owner_patch(tenant: TenantConfig, upload_id: str, row_index: int, changes: dict) -> dict:
    """Inline single-row fix (spec: re-validates ONLY that row)."""
    row = db.query_one(
        "SELECT * FROM upload_items WHERE upload_id=? AND row_index=?",
        (upload_id, row_index),
    )
    if row is None:
        raise RowNotFoundError(f"Row {row_index} not found in upload {upload_id}.")
    item = db.unj(row["fields_json"])

    for key, value in changes.items():
        if key not in PATCHABLE_FIELDS:
            continue
        if key == "price":
            price, _ = normalize_price(value)
            item["price"] = price
        elif key == "allergens" or key == "tags":
            if isinstance(value, str):
                item[key] = [v.strip() for v in value.split(",") if v.strip()]
            else:
                item[key] = list(value or [])
        elif key in ("is_featured", "is_available"):
            item[key] = value if isinstance(value, bool) else str(value).strip().lower() in ("yes", "true", "1")
        elif key == "sort_order":
            try:
                item["sort_order"] = int(value)
                item["sort_order_invalid"] = False
            except (TypeError, ValueError):
                item["sort_order"] = None
                item["sort_order_invalid"] = value not in (None, "")
        else:
            item[key] = (str(value).strip() or None) if value is not None else None
        item.setdefault("repairs", []).append(f"owner edit: {key}")

    # A fresh edit of the conflicting field reopens the question.
    if "price" in changes:
        item["conflict_dismissed"] = False

    db.execute(
        "UPDATE upload_items SET fields_json=?, updated_at=? WHERE upload_id=? AND row_index=?",
        (db.j(_clean_item(item)), utcnow(), upload_id, row_index),
    )
    audit(
        tenant.tenant_id,
        "owner",
        "row.edited",
        upload_id=upload_id,
        row=row_index,
        item_id=item["item_id"],
        fields=sorted(changes.keys()),
    )
    report = revalidate(tenant, upload_id)
    updated = db.query_one(
        "SELECT * FROM upload_items WHERE upload_id=? AND row_index=?", (upload_id, row_index)
    )
    return {
        "row_index": row_index,
        "item_id": updated["item_id"],
        "status": updated["status"],
        "report": report,
    }


def dismiss_photo_conflict(tenant: TenantConfig, upload_id: str, row_index: int) -> dict:
    """Owner confirms the spreadsheet value is right; the photo is stale."""
    row = db.query_one(
        "SELECT * FROM upload_items WHERE upload_id=? AND row_index=?", (upload_id, row_index)
    )
    if row is None:
        raise RowNotFoundError(f"Row {row_index} not found in upload {upload_id}.")
    item = db.unj(row["fields_json"])
    item["conflict_dismissed"] = True
    item.setdefault("repairs", []).append("owner confirmed spreadsheet price over photo")
    db.execute(
        "UPDATE upload_items SET fields_json=? WHERE upload_id=? AND row_index=?",
        (db.j(_clean_item(item)), upload_id, row_index),
    )
    audit(tenant.tenant_id, "owner", "photo_conflict.dismissed", upload_id=upload_id, row=row_index)
    return revalidate(tenant, upload_id)


def merge_partial_reupload(
    tenant: TenantConfig, upload_id: str, workbook_bytes: bytes, filename: str
) -> dict:
    """Partial re-upload merged by item_id (spec's second correction path).

    Only the columns present in the partial file overwrite the stored values;
    missing columns are left untouched. New items (unknown item_id) are appended.
    """
    _require_upload(upload_id)
    rows = read_sheet(workbook_bytes, filename)
    headers = list(rows[0].keys()) if rows else []
    mapping = build_mapping(headers, tenant, embeddings.similarity)

    existing = {item["item_id"]: item for _, item in _load_items(upload_id)}
    existing_rows = {item["item_id"]: row for row, item in _load_items(upload_id)}
    next_row = max(existing_rows.values(), default=0) + 1
    merged_ids: list[str] = []

    for row in rows:
        mapped = {mapping.mapping.get(h, h): v for h, v in row.items() if h in mapping.mapping}
        raw_id = (mapped.get("item_id") or "").strip() if mapped.get("item_id") else None
        target_id = raw_id
        if target_id and target_id in existing:
            item = existing[target_id]
            for key, value in mapped.items():
                if key not in PATCHABLE_FIELDS or value is None or str(value).strip() == "":
                    continue
                if key == "price":
                    price, _ = normalize_price(value)
                    item["price"] = price
                    item["conflict_dismissed"] = False
                elif key in ("allergens", "tags"):
                    item[key] = [v.strip() for v in str(value).split(",") if v.strip()]
                elif key in ("is_featured", "is_available"):
                    item[key] = str(value).strip().lower() in ("yes", "true", "1")
                elif key == "sort_order":
                    try:
                        item["sort_order"] = int(float(str(value)))
                    except ValueError:
                        item["sort_order_invalid"] = True
                else:
                    item[key] = str(value).strip()
            item.setdefault("repairs", []).append("partial re-upload merge")
            db.execute(
                "UPDATE upload_items SET fields_json=?, updated_at=? WHERE upload_id=? AND row_index=?",
                (db.j(_clean_item(item)), utcnow(), upload_id, existing_rows[target_id]),
            )
            merged_ids.append(target_id)
        else:
            # New item appended.
            projected = {k: v for k, v in mapped.items() if k in _KNOWN_FIELDS}
            new_item = auto_repair_row(projected, tenant)
            new_item["item_id"] = target_id
            new_item["source_row"] = len(rows)
            items = [new_item]
            apply_defaults_and_sort(items, tenant)
            if not new_item.get("item_id"):
                counter = len(existing) + 1
                new_item["item_id"] = f"MENU-{counter:04d}"
            if not new_item.get("description"):
                desc, method = llm_service.generate_description(new_item, tenant)
                new_item["description"] = desc
                new_item["description_auto_generated"] = True
                new_item["repairs"].append(f"description: blank -> auto-generated ({method})")
            db.execute(
                "INSERT INTO upload_items (upload_id, tenant_id, row_index, item_id,"
                " fields_json, status, updated_at) VALUES (?,?,?,?,?,?,?)",
                (
                    upload_id, tenant.tenant_id, next_row, new_item["item_id"],
                    db.j(_clean_item(new_item)), "valid", utcnow(),
                ),
            )
            next_row += 1

    audit(
        tenant.tenant_id, "owner", "partial_reupload.merged",
        upload_id=upload_id, merged=merged_ids, file=filename,
    )
    report = revalidate(tenant, upload_id)
    report["merged_item_ids"] = merged_ids
    return report


def add_item(tenant: TenantConfig, upload_id: str) -> dict:
    """Owner adds a new, blank menu item row to this pending upload.

    The row arrives empty; validation immediately flags the missing required
    fields and the owner fills them in with the inline editor (Save changes
    re-validates). The item_id is auto-generated in the MENU-#### style so a
    later partial re-upload can still merge by item_id.
    """
    _require_upload(upload_id)
    existing = _load_items(upload_id)
    next_row = max((row for row, _ in existing), default=0) + 1
    used = {item.get("item_id") for _, item in existing}
    n = 1
    while f"MENU-{n:04d}" in used:
        n += 1
    new_item = {
        "item_id": f"MENU-{n:04d}",
        "item_name": "",
        "price": None,
        "category": "",
        "is_featured": False,
        "is_available": True,
        "owner_added": True,
        "repairs": ["row added by owner from the dashboard"],
    }
    db.execute(
        "INSERT INTO upload_items (upload_id, tenant_id, row_index, item_id,"
        " fields_json, status, updated_at) VALUES (?,?,?,?,?,?,?)",
        (
            upload_id, tenant.tenant_id, next_row, new_item["item_id"],
            db.j(_clean_item(new_item)), "error", utcnow(),
        ),
    )
    audit(
        tenant.tenant_id, "owner", "item.added",
        upload_id=upload_id, row=next_row, item_id=new_item["item_id"],
    )
    report = revalidate(tenant, upload_id)
    report["added_row"] = next_row
    return report

def delete_item(tenant: TenantConfig, upload_id: str, row_index: int) -> dict:
    """Owner deletes a row outright (e.g. one of two duplicate dishes).

    Deleting only affects this pending upload — already-published versions
    are immutable. A photo assigned to the deleted row becomes an orphan
    again and can be re-assigned or ignored.
    """
    _require_upload(upload_id)
    item = db.query_one(
        "SELECT item_id FROM upload_items WHERE upload_id=? AND row_index=?",
        (upload_id, row_index),
    )
    if item is None:
        raise RowNotFoundError(f"Row {row_index} not found in this upload.")
    db.execute(
        "DELETE FROM upload_items WHERE upload_id=? AND row_index=?",
        (upload_id, row_index),
    )
    db.execute(
        "DELETE FROM validation_issues WHERE upload_id=? AND item_row=?",
        (upload_id, row_index),
    )
    db.execute(
        "UPDATE photos SET matched_row=NULL WHERE upload_id=? AND matched_row=?",
        (upload_id, row_index),
    )
    audit(
        tenant.tenant_id, "owner", "item.deleted",
        upload_id=upload_id, row=row_index, item_id=item["item_id"],
    )
    report = revalidate(tenant, upload_id)
    report["deleted_row"] = row_index
    return report

def assign_photo(tenant: TenantConfig, upload_id: str, filename: str, row_index: int) -> dict:
    """Owner assigns an unmatched (orphan) photo to a menu item."""
    photo = db.query_one(
        "SELECT * FROM photos WHERE upload_id=? AND filename=?", (upload_id, filename)
    )
    if photo is None:
        raise RowNotFoundError(f"Photo '{filename}' not found in this upload.")
    result = apply_owner_patch(tenant, upload_id, row_index, {"image_filename": filename})
    db.execute(
        "UPDATE photos SET matched_row=? WHERE upload_id=? AND filename=?",
        (row_index, upload_id, filename),
    )
    audit(
        tenant.tenant_id, "owner", "photo.assigned",
        upload_id=upload_id, filename=filename, row=row_index,
    )
    return result


# --------------------------------------------------------------------------
# Step 7: live preview (kiosk rendering data)
# --------------------------------------------------------------------------

def build_preview(tenant: TenantConfig, upload_id: str) -> dict:
    """The kiosk menu structure rendered from the same JSON that will publish."""
    items = _load_items(upload_id)
    categories: list[dict] = []
    by_category: dict[str, list] = {}
    for _, item in items:
        by_category.setdefault(item.get("category") or "Uncategorized", []).append(item)
    order = {c: i for i, c in enumerate(tenant.categories)}
    for category in sorted(by_category, key=lambda c: order.get(c, 999)):
        entries = sorted(by_category[category], key=lambda i: i.get("sort_order") or 0)
        categories.append(
            {
                "name": category,
                "items": [_preview_item(tenant, upload_id, i) for i in entries],
            }
        )
    featured = [
        _preview_item(tenant, upload_id, item)
        for _, item in items
        if item.get("is_featured")
    ]
    return {
        "upload_id": upload_id,
        "tenant": {
            "tenant_id": tenant.tenant_id,
            "restaurant_name": tenant.restaurant_name,
            "city": tenant.city,
        },
        "featured": featured,
        "categories": categories,
    }


def _preview_item(tenant: TenantConfig, upload_id: str, item: dict) -> dict:
    filename = item.get("image_filename") or ""
    return {
        "item_id": item["item_id"],
        "item_name": item.get("item_name") or "",
        "description": item.get("description") or "",
        "price": item.get("price"),
        "currency": tenant.currency,
        "dietary_type": item.get("dietary_type") or "",
        "spice_level": item.get("spice_level") or "None",
        "is_featured": bool(item.get("is_featured")),
        "is_available": bool(item.get("is_available")),
        "image_url": (
            f"/media/tenants/{tenant.tenant_id}/menu/uploads/{upload_id}/images/{filename}"
            if filename
            else None
        ),
    }


# --------------------------------------------------------------------------
# Step 8: publish (atomic version switch + downstream jobs)
# --------------------------------------------------------------------------

def publish(tenant: TenantConfig, upload_id: str, actor: str = "owner") -> dict:
    """Publish the upload as a new immutable menu version.

    Blocked while any hard error remains. Downstream jobs triggered:
      1. TTS generation for items — STUB (logged job; voice.status stays
         'not_generated' — no real TTS engine in the prototype).
      2. Image resize/compression — REAL (Pillow, 640px JPEG).
      3. Search/embedding index refresh — vectors stored per item.
    """
    report = revalidate(tenant, upload_id)
    if report["hard_errors"] > 0:
        raise PublishBlockedError(
            f"Publish is blocked: {report['hard_errors']} hard error(s) remain. "
            "Fix the rows marked Error and try again."
        )
    items = _load_items(upload_id)

    conn = db.connect()
    try:
        conn.execute("BEGIN")
        row = conn.execute(
            "SELECT COALESCE(MAX(version_no),0)+1 AS v FROM menu_versions WHERE tenant_id=?",
            (tenant.tenant_id,),
        ).fetchone()
        version_no = int(row["v"])
        records = []
        vectors = []
        for _, item in items:
            image_url = (
                f"/media/tenants/{tenant.tenant_id}/menu/menu-v{version_no}/images/"
                f"{item.get('image_filename') or ''}"
            )
            record, vector = build_canonical_record(item, tenant, version_no, image_url)
            records.append(record)
            vectors.append(vector)
            conn.execute(
                "INSERT INTO menu_items (tenant_id, version_no, item_id, record_json)"
                " VALUES (?,?,?,?)",
                (tenant.tenant_id, version_no, item["item_id"], db.j(record)),
            )
            conn.execute(
                "INSERT OR REPLACE INTO embeddings (tenant_id, item_id, model, vector_id,"
                " source_text, vector_json, version_no) VALUES (?,?,?,?,?,?,?)",
                (
                    tenant.tenant_id, item["item_id"], vector["model"], vector["vector_id"],
                    vector["source_text"], db.j(vector["vector"]), version_no,
                ),
            )
        conn.execute(
            "INSERT INTO menu_versions (tenant_id, version_no, upload_id, published_by,"
            " created_at, is_active) VALUES (?,?,?,?,?,0)",
            (tenant.tenant_id, version_no, upload_id, actor, utcnow()),
        )
        conn.execute(
            "INSERT INTO tenant_state (tenant_id, active_version) VALUES (?,?)"
            " ON CONFLICT(tenant_id) DO UPDATE SET active_version=excluded.active_version",
            (tenant.tenant_id, version_no),
        )
        conn.execute(
            "UPDATE menu_versions SET is_active=CASE WHEN version_no=? THEN 1 ELSE 0 END"
            " WHERE tenant_id=?",
            (version_no, tenant.tenant_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    document = {
        "tenant_id": tenant.tenant_id,
        "restaurant_name": tenant.restaurant_name,
        "version": version_no,
        "published_at": utcnow(),
        "published_by": actor,
        "item_count": len(records),
        "items": records,
    }
    storage.storage.write_menu_doc(tenant.tenant_id, version_no, document)

    # ---- Downstream jobs ---------------------------------------------------
    resized = _resize_images(tenant, upload_id, version_no, items)
    _stub_tts_job(tenant, version_no, records)
    _index_refresh_job(tenant, version_no, vectors)

    audit(
        tenant.tenant_id, actor, "menu.published",
        upload_id=upload_id, version=version_no, items=len(records), resized=resized,
    )
    return {"version": version_no, "item_count": len(records), "resized_images": resized}


def _resize_images(tenant: TenantConfig, upload_id: str, version_no: int, items) -> int:
    """REAL image processing: resize/compress to 640px JPEG for the kiosk."""
    from PIL import Image

    out_dir = storage.storage.resized_dir(tenant.tenant_id, version_no)
    count = 0
    for _, item in items:
        filename = item.get("image_filename")
        if not filename:
            continue
        path = storage.storage.photo_path(tenant.tenant_id, upload_id, filename)
        if path is None:
            continue
        try:
            with Image.open(path) as im:
                im = im.convert("RGB")
                im.thumbnail((640, 640))
                target = out_dir / (path.stem + ".jpg")
                im.save(target, "JPEG", quality=82, optimize=True)
                count += 1
        except Exception:  # noqa: BLE001 - a bad image must not fail the publish
            continue
    db.execute(
        "INSERT INTO jobs (tenant_id, version_no, job_type, status, detail, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (tenant.tenant_id, version_no, "image_resize", "done", f"{count} images resized", utcnow()),
    )
    return count


def _stub_tts_job(tenant: TenantConfig, version_no: int, records: list[dict]) -> None:
    """TTS is stubbed: the pipeline stage exists (job logged), no audio generated.

    PRODUCTION NOTE: replace with a batch call to the TTS service
    (e.g. OpenAI speech, Amazon Polly) writing mp3s to object storage and
    flipping voice.status to 'generated' with the audio URL.
    """
    db.execute(
        "INSERT INTO jobs (tenant_id, version_no, job_type, status, detail, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (
            tenant.tenant_id, version_no, "tts", "stubbed",
            f"queued {len(records)} items for narration (stub — no TTS engine in prototype)",
            utcnow(),
        ),
    )


def _index_refresh_job(tenant: TenantConfig, version_no: int, vectors: list[dict]) -> None:
    db.execute(
        "INSERT INTO jobs (tenant_id, version_no, job_type, status, detail, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (tenant.tenant_id, version_no, "index_refresh", "done",
         f"{len(vectors)} embeddings stored", utcnow()),
    )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _require_upload(upload_id: str):
    upload = db.query_one("SELECT * FROM uploads WHERE id=?", (upload_id,))
    if upload is None:
        raise UploadNotFoundError(f"Upload '{upload_id}' not found.")
    return upload
