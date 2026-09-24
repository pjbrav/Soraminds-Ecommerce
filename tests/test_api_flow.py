"""API flow tests: upload -> report -> inline edit (re-validate) -> publish.

Covers the re-validate-after-edit path, the publish blocking rule, the
partial re-upload merge, orphan-photo assignment, featured endpoint, and
rollback — all through the real HTTP surface via TestClient.
"""
from __future__ import annotations

import io

from fastapi.testclient import TestClient
from openpyxl import Workbook
from PIL import Image

from app.main import app

client = TestClient(app)
TENANT = "spicehub-kitchen-troy"

HEADERS = [
    "item_id", "category", "subcategory", "item_name", "description", "price",
    "dietary_type", "spice_level", "calorie_range", "allergens", "image_filename",
    "is_featured", "is_available", "sort_order", "tags",
]


def make_menu_xlsx(rows: list[dict]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Menu Items"
    ws.append(HEADERS)
    for row in rows:
        ws.append([row.get(h) for h in HEADERS])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def make_jpeg(name: str = "test") -> bytes:
    img = Image.new("RGB", (64, 64), (200, 100, 100))
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    return buf.getvalue()


def rows_fixture() -> list[dict]:
    """Two valid rows + one broken row (missing price, bad category)."""
    return [
        {"item_id": "MENU-0001", "category": "Breads", "item_name": "Naan",
         "description": "Indian flat bread.", "price": 1.99, "dietary_type": "Veg",
         "spice_level": "None", "image_filename": "naan.jpg", "is_featured": "No",
         "is_available": "Yes"},
        {"item_id": "MENU-0002", "category": "Breads", "item_name": "Garlic Naan",
         "description": "Flat bread with garlic.", "price": 2.49, "dietary_type": "Veg",
         "spice_level": "None", "image_filename": "garlic_naan.jpg", "is_featured": "No",
         "is_available": "Yes"},
        {"item_id": "MENU-0003", "category": "Currys", "item_name": "Butter Chicken",
         "description": "Chicken in creamy tomato sauce.", "price": None,
         "dietary_type": "Non-Veg", "spice_level": "Mild",
         "image_filename": "butter_chicken.jpg", "is_featured": "Yes", "is_available": "Yes"},
    ]


def upload(rows: list[dict], photos: dict[str, bytes] | None = None) -> dict:
    files = [("file", ("menu.xlsx", make_menu_xlsx(rows), "application/octet-stream"))]
    for name, data in (photos or {
        "naan.jpg": make_jpeg(), "garlic_naan.jpg": make_jpeg(), "butter_chicken.jpg": make_jpeg(),
    }).items():
        files.append(("photos", (name, data, "image/jpeg")))
    resp = client.post(f"/tenants/{TENANT}/menu/upload", files=files)
    assert resp.status_code == 201, resp.text
    return resp.json()


def report(upload_id: str) -> dict:
    resp = client.get(f"/tenants/{TENANT}/menu/uploads/{upload_id}/validation-report")
    assert resp.status_code == 200
    return resp.json()


def patch_row(upload_id: str, row: int, changes: dict) -> dict:
    resp = client.patch(
        f"/tenants/{TENANT}/menu/uploads/{upload_id}/items/{row}", json={"changes": changes}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# --------------------------------------------------------------------------

def test_upload_reports_all_rows_and_issues():
    data = upload(rows_fixture())
    rep = report(data["uploadId"])
    assert rep["total_rows"] == 3
    by_row = {r["row_index"]: r for r in rep["rows"]}
    assert by_row[1]["status"] == "valid"
    assert by_row[3]["status"] == "error"  # missing price AND bad category
    codes = {i["code"] for i in by_row[3]["issues"]}
    assert "price" in codes and "category" in codes


def test_inline_edit_revalidates_only_that_row():
    data = upload(rows_fixture())
    upload_id = data["uploadId"]

    # Fix the broken row: price + category.
    result = patch_row(upload_id, 3, {"price": 13.99, "category": "Non Veg Curries"})
    assert result["status"] == "valid"

    rep = report(upload_id)
    assert all(r["status"] == "valid" for r in rep["rows"])
    assert rep["hard_errors"] == 0 and rep["publishable"] is True


def test_edit_can_also_break_a_row_and_report_it():
    data = upload(rows_fixture())
    upload_id = data["uploadId"]
    patch_row(upload_id, 3, {"price": 13.99, "category": "Non Veg Curries"})
    result = patch_row(upload_id, 1, {"price": 999})  # above the sanity ceiling
    assert result["status"] == "error"


def test_publish_blocked_with_hard_errors():
    data = upload(rows_fixture())
    resp = client.post(f"/tenants/{TENANT}/menu/uploads/{data['uploadId']}/publish")
    assert resp.status_code == 409
    assert "blocked" in resp.json()["error"]["message"].lower()


def test_preview_unavailable_with_hard_errors():
    data = upload(rows_fixture())
    resp = client.post(f"/tenants/{TENANT}/menu/uploads/{data['uploadId']}/preview")
    assert resp.status_code == 409


def test_publish_after_fixes_creates_versioned_active_menu():
    data = upload(rows_fixture())
    upload_id = data["uploadId"]
    patch_row(upload_id, 3, {"price": 13.99, "category": "Non Veg Curries"})

    preview = client.post(f"/tenants/{TENANT}/menu/uploads/{upload_id}/preview")
    assert preview.status_code == 200
    assert any(i["is_featured"] for i in preview.json()["featured"])

    resp = client.post(f"/tenants/{TENANT}/menu/uploads/{upload_id}/publish")
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == 1

    active = client.get(f"/tenants/{TENANT}/menu/active").json()
    assert active["version"] == 1 and active["item_count"] == 3
    record = active["items"][0]
    # spec §7 schema shape
    for key in ("item_id", "tenant_id", "price", "currency", "image", "embedding",
                "voice", "runtime_stats", "menu_version"):
        assert key in record, f"missing {key}"
    assert record["runtime_stats"]["popularity_rank"] is None
    assert record["voice"]["status"] == "not_generated"

    featured = client.get(f"/tenants/{TENANT}/menu/featured").json()
    assert featured["count"] == 1
    assert featured["items"][0]["item_name"] == "Butter Chicken"


def test_second_publish_increments_version_rollback_restores():
    data = upload(rows_fixture())
    upload_id = data["uploadId"]
    patch_row(upload_id, 3, {"price": 13.99, "category": "Non Veg Curries"})
    client.post(f"/tenants/{TENANT}/menu/uploads/{upload_id}/publish")  # v1
    rows = rows_fixture()
    rows[2]["price"] = 15.99
    data2 = upload(rows)
    patch_row(data2["uploadId"], 3, {"category": "Non Veg Curries"})
    resp = client.post(f"/tenants/{TENANT}/menu/uploads/{data2['uploadId']}/publish")
    assert resp.json()["version"] == 2  # never overwrites v1

    active = client.get(f"/tenants/{TENANT}/menu/active").json()
    price = next(i["price"] for i in active["items"] if i["item_id"] == "MENU-0003")
    assert price == 15.99

    resp = client.post(f"/tenants/{TENANT}/menu/versions/1/rollback")
    assert resp.status_code == 200
    active = client.get(f"/tenants/{TENANT}/menu/active").json()
    price = next(i["price"] for i in active["items"] if i["item_id"] == "MENU-0003")
    assert price == 13.99  # v1 preserved byte-for-byte


def test_partial_reupload_merges_by_item_id():
    data = upload(rows_fixture())
    upload_id = data["uploadId"]
    patch_row(upload_id, 3, {"price": 13.99, "category": "Non Veg Curries"})

    partial_rows = [{"item_id": "MENU-0002", "price": 3.49}]
    files = {"file": ("partial.xlsx", make_menu_xlsx(partial_rows), "application/octet-stream")}
    resp = client.post(
        f"/tenants/{TENANT}/menu/uploads/{upload_id}/partial-reupload", files=files
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["merged_item_ids"] == ["MENU-0002"]

    rep = report(upload_id)
    row2 = next(r for r in rep["rows"] if r["item_id"] == "MENU-0002")
    assert row2["fields"]["price"] == 3.49
    assert len(rep["rows"]) == 3  # no duplicate rows appended


def test_orphan_photo_assignment_resolves_missing_photo_issue():
    rows = rows_fixture()
    rows[1]["image_filename"] = "missing.jpg"  # photo exists under another name
    data = upload(rows, photos={
        "naan.jpg": make_jpeg(), "butter_chicken.jpg": make_jpeg(), "orphan.jpg": make_jpeg(),
    })
    upload_id = data["uploadId"]
    rep = report(upload_id)
    assert any(i["code"] == "photo_unmatched" for i in rep["upload_issues"])
    assert any(i["code"] == "image_filename" for r in rep["rows"] if r["row_index"] == 2 for i in r["issues"])

    resp = client.post(
        f"/tenants/{TENANT}/menu/uploads/{upload_id}/photos/orphan.jpg/assign",
        json={"row_index": 2},
    )
    assert resp.status_code == 200
    rep = report(upload_id)
    row2 = next(r for r in rep["rows"] if r["row_index"] == 2)
    assert row2["fields"]["image_filename"] == "orphan.jpg"
    assert not any(
        i["code"] == "photo_unmatched" and i["extra"]["filename"] == "orphan.jpg"
        for i in rep["upload_issues"]
    )


def test_bad_file_types_rejected_before_parsing():
    files = {"file": ("menu.txt", b"just text", "text/plain")}
    resp = client.post(f"/tenants/{TENANT}/menu/upload", files=files)
    assert resp.status_code == 400

    files = {"file": ("menu.xlsx", b"not really a zip", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    resp = client.post(f"/tenants/{TENANT}/menu/upload", files=files)
    assert resp.status_code == 400
    assert "signature" in resp.json()["error"]["message"]


def test_unknown_tenant_404():
    files = {"file": ("menu.xlsx", b"x", "application/zip")}
    resp = client.post("/tenants/nope/menu/upload", files=files)
    assert resp.status_code == 404
