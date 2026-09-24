"""End-to-end demo run: drives the real API from upload to published kiosk JSON.

Two paths (README quickstart):
  1. Messy path — demo/messy_menu.xlsx + photos.zip, then owner fixes applied
     through the same PATCH/assign endpoints the dashboard uses, then publish.
  2. Clean path — demo/clean_menu.xlsx, publish directly.

The published kiosk-ready JSON documents are written to demo/output/.

Usage:  python scripts/demo_run.py
"""
from __future__ import annotations

# Some sandboxed CI environments cap process virtual memory below what torch
# needs; raise the soft limit when the hard limit allows it.
try:
    import resource
except ImportError:  # Windows: stdlib 'resource' does not exist -> harmless stub
    import types
    resource = types.SimpleNamespace(
        RLIMIT_AS=0,
        RLIM_INFINITY=-1,
        getrlimit=lambda *a: (0, -1),
        setrlimit=lambda *a: None,
    )

try:
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    if soft != resource.RLIM_INFINITY and hard == resource.RLIM_INFINITY:
        resource.setrlimit(resource.RLIMIT_AS, (resource.RLIM_INFINITY, resource.RLIM_INFINITY))
except Exception:  # noqa: BLE001
    pass

import json
import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RUN_DIR = REPO / "demo" / "_run"
OUT_DIR = REPO / "demo" / "output"

os.environ["SPICEHUB_DATA_DIR"] = str(RUN_DIR / "data")
os.environ["SPICEHUB_STORAGE_ROOT"] = str(RUN_DIR / "storage")

sys.path.insert(0, str(REPO))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

TENANT = "spicehub-kitchen-troy"
LOG_LINES: list[str] = []


def log(line: str = "") -> None:
    print(line)
    LOG_LINES.append(line)


def upload(client: TestClient, menu_file: Path, photos_zip: str = "photos.zip") -> str:
    with open(menu_file, "rb") as f, open(REPO / "demo" / photos_zip, "rb") as z:
        resp = client.post(
            f"/tenants/{TENANT}/menu/upload",
            files=[
                ("file", (menu_file.name, f, "application/octet-stream")),
                ("photos", ("photos.zip", z, "application/zip")),
            ],
        )
    if resp.status_code != 201:
        log(f"UPLOAD FAILED: {resp.status_code} {resp.text}")
        raise SystemExit(1)
    data = resp.json()
    log(
        f"uploaded {menu_file.name} -> uploadId {data['uploadId']} | "
        f"{data['rows']} items | {data['counts']['error']} errors, "
        f"{data['counts']['warning']} warnings, {data['counts']['valid']} valid | "
        f"vision: {data.get('visionProvider')}"
    )
    return data["uploadId"]


def report(client: TestClient, upload_id: str) -> dict:
    return client.get(f"/tenants/{TENANT}/menu/uploads/{upload_id}/validation-report").json()


def show_report(client: TestClient, upload_id: str, only_problems: bool = True) -> None:
    rep = report(client, upload_id)
    log(f"  counts: {rep['counts']} | hard errors: {rep['hard_errors']} | publishable: {rep['publishable']}")
    for issue in rep["upload_issues"]:
        log(f"  [upload] {issue['severity']}: {issue['message']}")
    for row in rep["rows"]:
        if only_problems and row["status"] == "valid":
            continue
        log(f"  row {row['row_index']:>2} {row['status'].upper():>7} {row['fields']['item_name']}")
        for i in row["issues"]:
            log(f"          - {i['severity']}/{i['code']}: {i['message']}")
    if rep["mapping"] and rep["mapping"].get("auto_mapped"):
        log(f"  auto-mapped columns: {rep['mapping']['auto_mapped']}")


def patch(client: TestClient, upload_id: str, row: int, changes: dict, note: str) -> dict:
    resp = client.patch(
        f"/tenants/{TENANT}/menu/uploads/{upload_id}/items/{row}", json={"changes": changes}
    )
    if resp.status_code != 200:
        log(f"PATCH FAILED row {row}: {resp.status_code} {resp.text}")
        raise SystemExit(1)
    data = resp.json()
    log(f"  [owner edit] row {row}: {note} -> status now '{data['status']}'")
    return data


def find_row(rep: dict, item_name: str) -> int:
    for row in rep["rows"]:
        if row["fields"]["item_name"] == item_name:
            return row["row_index"]
    raise KeyError(item_name)


def main() -> None:
    if RUN_DIR.exists():
        shutil.rmtree(RUN_DIR)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    client = TestClient(app)

    # ================= Path 1: the messy menu =================
    log("=" * 72)
    log("PATH 1 — messy menu (owner-resolvable issues end to end)")
    log("=" * 72)
    upload_id = upload(client, REPO / "demo" / "messy_menu.xlsx")
    show_report(client, upload_id)

    log("")
    log("--- Owner resolves every blocking issue via the dashboard/API ---")
    rep = report(client, upload_id)

    # Missing price -> owner enters the price.
    patch(client, upload_id, find_row(rep, "Tandoori Shrimp"),
          {"price": 13.99}, "entered missing price 13.99")

    # Ambiguous category -> owner picks the correct one.
    patch(client, upload_id, find_row(rep, "Butter Chicken"),
          {"category": "Non Veg Curries"}, "'Currys' -> 'Non Veg Curries'")

    # Missing photo -> owner assigns the orphan photo to the row.
    rasmalai = find_row(rep, "Rasmalai (3)")
    resp = client.post(
        f"/tenants/{TENANT}/menu/uploads/{upload_id}/photos/chef_special.jpg/assign",
        json={"row_index": rasmalai},
    )
    log(f"  [owner edit] assigned orphan photo chef_special.jpg -> 'Rasmalai (3)'"
        f" (status {resp.json()['status'] if resp.status_code == 200 else resp.text})")

    # Photo/sheet price conflicts -> owner picks which source is right.
    # (two conflicts exist: Chicken Biryani 11.99-vs-13.99, and Chilli Paneer
    # 14.99-vs-13.99 — the latter created by the "$14,99" auto-repair)
    conflict_rows = [r for r in rep["rows"] if any(i["code"] == "photo_conflict" for i in r["issues"])]
    for conflict_row in conflict_rows:
        issue = next(i for i in conflict_row["issues"] if i["code"] == "photo_conflict")
        photo_price = issue["extra"]["photo_price"]
        if conflict_row["fields"]["item_name"] == "Chicken Biryani":
            patch(client, upload_id, conflict_row["row_index"],
                  {"price": photo_price}, f"photo price ${photo_price:.2f} wins over sheet")
        else:
            resp = client.post(
                f"/tenants/{TENANT}/menu/uploads/{upload_id}/items/{conflict_row['row_index']}/dismiss-conflict"
            )
            log(f"  [owner edit] {conflict_row['fields']['item_name']}: kept spreadsheet "
                f"${issue['extra']['sheet_price']:.2f} (photo is stale) -> "
                f"{resp.json()['counts'] if resp.status_code == 200 else resp.text}")

    # Malformed calorie range + unknown allergen.
    patch(client, upload_id, find_row(rep, "Chicken Biryani"),
          {"calorie_range": "550-700"}, "fixed calorie range to 550-700")
    patch(client, upload_id, find_row(rep, "Naan"),
          {"allergens": "Gluten"}, "removed unknown allergen 'Spices'")

    # Correction path (b): partial re-upload merged by item_id.
    # The owner renames the near-duplicate 'Paneer Biriyani' via a one-row
    # spreadsheet instead of editing inline.
    from openpyxl import Workbook

    dup_row = next(r for r in rep["rows"] if r["fields"]["item_name"] == "Paneer Biriyani")
    partial = Workbook()
    sheet = partial.active
    sheet.append(["item_id", "item_name", "description"])
    sheet.append([dup_row["item_id"], "Paneer Biryani (Chef's Style)",
                  "Fragrant basmati layered with paneer, saffron and whole spices, dum-cooked."])
    partial_path = RUN_DIR / "partial_fix.xlsx"
    partial.save(partial_path)
    with open(partial_path, "rb") as f:
        resp = client.post(
            f"/tenants/{TENANT}/menu/uploads/{upload_id}/partial-reupload",
            files={"file": ("partial_fix.xlsx", f, "application/octet-stream")},
        )
    log(f"  [partial re-upload] renamed near-duplicate row via item_id {dup_row['item_id']} "
        f"-> merged: {resp.json().get('merged_item_ids')}")
    if resp.status_code != 200 or not resp.json().get("merged_item_ids"):
        raise SystemExit(f"Partial re-upload failed: {resp.status_code} {resp.text}")

    log("")
    log("--- Re-validate after owner corrections ---")
    rep = report(client, upload_id)
    log(f"  counts: {rep['counts']} | hard errors: {rep['hard_errors']} | publishable: {rep['publishable']}")
    for row in rep["rows"]:
        if row["status"] == "error":
            raise SystemExit(f"Unexpected hard error remains: {row}")

    # Live preview + publish.
    resp = client.post(f"/tenants/{TENANT}/menu/uploads/{upload_id}/preview")
    preview = resp.json()
    log("")
    log("--- Live preview (same JSON that will publish) ---")
    log(f"  featured strip: {len(preview['featured'])} items; "
        f"categories: {len(preview['categories'])} ({', '.join(c['name'] for c in preview['categories'][:3])}...)")

    resp = client.post(f"/tenants/{TENANT}/menu/uploads/{upload_id}/publish")
    log(f"  publish -> {resp.json()}")
    version_1 = resp.json()["version"]

    active = client.get(f"/tenants/{TENANT}/menu/active").json()
    featured = client.get(f"/tenants/{TENANT}/menu/featured").json()
    log(f"  active menu: v{active['version']} with {active['item_count']} items; "
        f"featured endpoint returns {featured['count']}")

    out = OUT_DIR / f"kiosk_menu_v{version_1}_messy_fixed.json"
    out.write_text(json.dumps(active, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"  kiosk-ready JSON written to {out}")

    # ================= Path 2: the clean menu =================
    log("")
    log("=" * 72)
    log("PATH 2 — clean menu (happy path, direct publish)")
    log("=" * 72)
    clean_id = upload(client, REPO / "demo" / "clean_menu.xlsx", photos_zip="photos_clean.zip")
    show_report(client, clean_id, only_problems=False)
    rep = report(client, clean_id)
    if rep["hard_errors"] != 0:
        raise SystemExit(f"Clean path unexpectedly has {rep['hard_errors']} hard errors")
    resp = client.post(f"/tenants/{TENANT}/menu/uploads/{clean_id}/publish")
    log(f"  publish -> {resp.json()}")
    version_2 = resp.json()["version"]
    active = client.get(f"/tenants/{TENANT}/menu/active").json()
    out = OUT_DIR / f"kiosk_menu_v{version_2}_clean.json"
    out.write_text(json.dumps(active, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"  kiosk-ready JSON written to {out}")

    # Rollback demo: roll back to v1 and forward again.
    log("")
    log("--- Version history + rollback ---")
    resp = client.post(f"/tenants/{TENANT}/menu/versions/{version_1}/rollback")
    log(f"  rollback to v{version_1}: {resp.json()['message']}")
    resp = client.post(f"/tenants/{TENANT}/menu/versions/{version_2}/rollback")
    log(f"  rollback to v{version_2}: {resp.json()['message']}")

    # Usage accounting demo.
    from app import db

    usage = db.query("SELECT * FROM llm_usage ORDER BY id")
    log("")
    log(f"LLM usage rows: {len(usage)} "
        f"(descriptions use the deterministic fallback unless OPENAI_API_KEY is set)")
    audits = db.query("SELECT action, COUNT(*) n FROM audit_log GROUP BY action ORDER BY n DESC")
    log("Audit log actions: " + ", ".join(f"{a['action']}x{a['n']}" for a in audits))

    log("")
    log("Demo complete. Dashboard equivalents of every step above live at /")
    (OUT_DIR / "demo_run_log.txt").write_text("\n".join(LOG_LINES) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
