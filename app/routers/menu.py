"""Menu API (spec §8 contract)."""
from __future__ import annotations

from fastapi import APIRouter, File, Request, UploadFile

from .. import db, pipeline, security, storage
from ..audit import audit, utcnow
from ..errors import AppError, BadFileError, ConflictError
from ..models import (
    AssignPhotoRequest,
    PatchItemRequest,
    PublishResponse,
    RollbackResponse,
    UploadResponse,
)
from ..tenant_config import TenantConfig, get_tenant

router = APIRouter(prefix="/tenants/{tenant_id}/menu", tags=["menu"])


def _tenant(tenant_id: str) -> TenantConfig:
    return get_tenant(tenant_id)  # raises TenantNotFoundError -> 404


# --------------------------------------------------------------------------
# POST /tenants/{id}/menu/upload
# --------------------------------------------------------------------------

@router.post("/upload", response_model=UploadResponse, status_code=201)
async def upload_menu(
    tenant_id: str,
    request: Request,
    file: UploadFile = File(..., description="Menu spreadsheet (.xlsx or .csv)"),
    photos: list[UploadFile] | None = None,
):
    """Step 1 (File Intake) + Steps 2-5, synchronously. Returns uploadId.

    Security order: extension -> declared MIME -> magic bytes -> size ->
    malware scan (stub) -> only then any parser opens the file.
    """
    tenant = _tenant(tenant_id)
    data = await file.read()
    security.check_menu_file(file.filename, file.content_type, data, tenant.limit("max_menu_file_mb", 5))
    security.malware_scanner.scan(file.filename or "menu", data)

    upload_id = pipeline.new_upload_id()
    db.execute(
        "INSERT INTO uploads (id, tenant_id, filename, status, created_at) VALUES (?,?,?,?,?)",
        (upload_id, tenant.tenant_id, file.filename or "upload", "processing", utcnow()),
    )
    storage.storage.save_raw_upload(tenant.tenant_id, upload_id, file.filename or "menu", data)

    saved_photos = await _store_photos(tenant, upload_id, photos or [])
    if saved_photos["rejected"]:
        db.execute("UPDATE uploads SET status='failed' WHERE id=?", (upload_id,))
        raise BadFileError("; ".join(saved_photos["rejected"]))

    summary = pipeline.process_upload(tenant, upload_id, data, file.filename or "menu.xlsx")
    return UploadResponse(
        uploadId=upload_id,
        tenantId=tenant.tenant_id,
        rows=summary["total_rows"],
        counts=summary["counts"],
        hardErrors=summary["hard_errors"],
        publishable=summary["publishable"],
        visionProvider=summary.get("vision_provider"),
        message="Upload processed. Review the validation report next.",
    )


async def _store_photos(tenant: TenantConfig, upload_id: str, photos: list[UploadFile]) -> dict:
    """Accept individual image files and/or .zip archives of them."""
    saved, rejected = [], []
    total_mb_limit = tenant.limit("max_images_total_mb", 200)
    seen: set[str] = set()
    total_bytes = 0
    for photo in photos:
        if photo.filename is None:
            continue
        data = await photo.read()
        name = photo.filename
        if name.lower().endswith(".zip"):
            try:
                extracted = storage.extract_zip(data)
            except BadFileError as exc:
                rejected.append(str(exc))
                continue
            for inner_name, inner_data in extracted.items():
                _save_one_photo(tenant, upload_id, inner_name, inner_data, saved, rejected, seen)
        else:
            _save_one_photo(tenant, upload_id, name, data, saved, rejected, seen)
        total_bytes += len(data)
        if total_bytes > total_mb_limit * 1024 * 1024:
            rejected.append(f"Total image upload exceeds {total_mb_limit} MB.")
            break
    return {"saved": saved, "rejected": rejected}


def _save_one_photo(tenant, upload_id, name, data, saved, rejected, seen: set) -> None:
    if not data:
        return
    try:
        security.check_image_file(name, data, tenant.limit("max_image_mb", 8))
        security.malware_scanner.scan(name, data)
        security.validate_image_bytes(name, data)
    except BadFileError as exc:
        rejected.append(str(exc))
        return
    if name in seen:
        return
    seen.add(name)
    storage.storage.save_photo(tenant.tenant_id, upload_id, name, data)
    db.execute(
        "INSERT OR IGNORE INTO photos (upload_id, tenant_id, filename, created_at)"
        " VALUES (?,?,?,?)",
        (upload_id, tenant.tenant_id, name, utcnow()),
    )
    saved.append(name)


# --------------------------------------------------------------------------
# GET validation report / PATCH row / preview / publish
# --------------------------------------------------------------------------

@router.get("/uploads/{upload_id}/validation-report")
def validation_report(tenant_id: str, upload_id: str):
    _tenant(tenant_id)
    return pipeline.get_report(tenant_id, upload_id)


@router.patch("/uploads/{upload_id}/items/{row_index}")
def patch_item(tenant_id: str, upload_id: str, row_index: int, body: PatchItemRequest):
    """Inline single-row fix; re-validates only that row (plus menu warnings)."""
    tenant = _tenant(tenant_id)
    if not body.changes:
        raise AppError("No changes supplied in the request body.")
    return pipeline.apply_owner_patch(tenant, upload_id, row_index, body.changes)


@router.post("/uploads/{upload_id}/items/{row_index}/dismiss-conflict")
def dismiss_conflict(tenant_id: str, upload_id: str, row_index: int):
    """Owner confirms the spreadsheet value; the photo's price is outdated."""
    tenant = _tenant(tenant_id)
    return pipeline.dismiss_photo_conflict(tenant, upload_id, row_index)


@router.post("/uploads/{upload_id}/photos/{filename}/assign")
def assign_photo(tenant_id: str, upload_id: str, filename: str, body: AssignPhotoRequest):
    """Owner assigns an unmatched (orphan) photo to a menu item."""
    tenant = _tenant(tenant_id)
    return pipeline.assign_photo(tenant, upload_id, filename, body.row_index)


@router.post("/uploads/{upload_id}/preview")
def preview(tenant_id: str, upload_id: str):
    """Step 7: kiosk preview data. 409 while any hard error remains."""
    tenant = _tenant(tenant_id)
    report = pipeline.get_report(tenant_id, upload_id)
    if report["hard_errors"] > 0:
        raise ConflictError(
            f"Preview is unavailable: {report['hard_errors']} row(s) still have errors. "
            "Fix them in the validation report first."
        )
    data = pipeline.build_preview(tenant, upload_id)
    data["counts"] = report["counts"]
    return data


@router.post("/uploads/{upload_id}/partial-reupload")
async def partial_reupload(
    tenant_id: str,
    upload_id: str,
    file: UploadFile = File(..., description="Partial menu (.xlsx/.csv) keyed by item_id"),
):
    """Correction path (b): merge a partial file into the upload by item_id."""
    tenant = _tenant(tenant_id)
    data = await file.read()
    security.check_menu_file(file.filename, file.content_type, data, tenant.limit("max_menu_file_mb", 5))
    security.malware_scanner.scan(file.filename or "partial", data)
    return pipeline.merge_partial_reupload(tenant, upload_id, data, file.filename or "partial.xlsx")


@router.post("/uploads/{upload_id}/publish", response_model=PublishResponse)
def publish(tenant_id: str, upload_id: str):
    """Step 8: atomic version switch + downstream jobs. 409 while blocked."""
    tenant = _tenant(tenant_id)
    result = pipeline.publish(tenant, upload_id)
    return PublishResponse(
        version=result["version"],
        item_count=result["item_count"],
        resized_images=result["resized_images"],
        message=f"Published menu-v{result['version']} — it is now the active menu.",
    )


# --------------------------------------------------------------------------
# Active menu / featured / versions / rollback
# --------------------------------------------------------------------------

@router.get("/active")
def active_menu(tenant_id: str):
    """Current live structured menu (the published versioned JSON document)."""
    _tenant(tenant_id)
    doc = _active_document(tenant_id)
    if doc is None:
        raise AppError("No menu published yet for this tenant.", code="no_active_menu")
    return doc


@router.get("/featured")
def featured(tenant_id: str):
    """Featured Items strip: filters is_featured=true from the active menu."""
    _tenant(tenant_id)
    doc = _active_document(tenant_id)
    if doc is None:
        raise AppError("No menu published yet for this tenant.", code="no_active_menu")
    items = [i for i in doc["items"] if i.get("is_featured")]
    return {
        "tenant_id": doc["tenant_id"],
        "version": doc["version"],
        "count": len(items),
        "items": items,
    }


@router.get("/versions")
def versions(tenant_id: str):
    _tenant(tenant_id)
    rows = db.query(
        "SELECT version_no, upload_id, published_by, created_at, is_active"
        " FROM menu_versions WHERE tenant_id=? ORDER BY version_no DESC",
        (tenant_id,),
    )
    return {"versions": [dict(r) for r in rows]}


@router.get("/versions/{version_no}/doc")
def version_doc(tenant_id: str, version_no: int):
    """Raw published JSON document for one version (never mutated after publish)."""
    _tenant(tenant_id)
    doc = storage.storage.read_menu_doc(tenant_id, version_no)
    if doc is None:
        raise AppError(f"Version menu-v{version_no} does not exist.")
    return doc


@router.post("/versions/{version_no}/rollback", response_model=RollbackResponse)
def rollback(tenant_id: str, version_no: int):
    _tenant(tenant_id)
    row = db.query_one(
        "SELECT * FROM menu_versions WHERE tenant_id=? AND version_no=?",
        (tenant_id, version_no),
    )
    if row is None:
        raise AppError(f"Version menu-v{version_no} does not exist.")
    db.execute(
        "INSERT INTO tenant_state (tenant_id, active_version) VALUES (?,?)"
        " ON CONFLICT(tenant_id) DO UPDATE SET active_version=excluded.active_version",
        (tenant_id, version_no),
    )
    db.execute(
        "UPDATE menu_versions SET is_active=CASE WHEN version_no=? THEN 1 ELSE 0 END"
        " WHERE tenant_id=?",
        (version_no, tenant_id),
    )
    audit(tenant_id, "owner", "menu.rolled_back", version=version_no)
    return RollbackResponse(
        active_version=version_no,
        message=f"Rolled back to menu-v{version_no}.",
    )


def _active_document(tenant_id: str) -> dict | None:
    state = db.query_one("SELECT active_version FROM tenant_state WHERE tenant_id=?", (tenant_id,))
    if state is None or state["active_version"] is None:
        return None
    return storage.storage.read_menu_doc(tenant_id, state["active_version"])
