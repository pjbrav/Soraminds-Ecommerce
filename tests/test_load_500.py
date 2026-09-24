"""Worst-case load test (spec checklist #16): 500 rows, EVERY row erroneous.

The pipeline must complete quickly and report every row's error — never
stopping at the first failure, never timing out.
"""
from __future__ import annotations

import time

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
TENANT = "spicehub-kitchen-troy"

from tests.test_api_flow import make_menu_xlsx  # noqa: E402  (shared helper)


def test_500_rows_all_erroneous_reports_every_error():
    rows = []
    for i in range(500):
        rows.append(
            {
                # Every single row is broken, with a variety of faults:
                "item_id": f"MENU-{i + 1:04d}",
                "category": "Currys" if i % 3 == 0 else "Totally Unknown Category",  # bad category
                "item_name": "" if i % 5 == 0 else f"Dish {i} " + "x" * 60,  # empty or > 60 chars
                "price": None if i % 2 == 0 else -1,                          # missing or negative
                "dietary_type": "Carnivore",                                  # invalid dropdown
                "spice_level": "Nuclear",                                    # invalid dropdown
                "calorie_range": "lots",                                      # malformed regex
                "allergens": "Dust, Sunshine",                                # unknown allergens
                "image_filename": f"photo_{i}.jpg",                          # no photos uploaded
                "is_featured": "Yes",
            }
        )
    files = {"file": ("big.xlsx", make_menu_xlsx(rows), "application/octet-stream")}
    started = time.perf_counter()
    resp = client.post(f"/tenants/{TENANT}/menu/upload", files=files)
    elapsed = time.perf_counter() - started
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["rows"] == 500
    assert data["counts"]["error"] == 500  # every row erroneous
    assert data["hardErrors"] >= 500

    rep = client.get(
        f"/tenants/{TENANT}/menu/uploads/{data['uploadId']}/validation-report"
    ).json()
    assert rep["total_rows"] == 500
    # collect-all guarantee: each row carries multiple distinct issues
    sample = rep["rows"][0]
    assert len(sample["issues"]) >= 3
    # must complete fast (spec: load test with every row containing an error)
    assert elapsed < 30, f"500-row load test took {elapsed:.1f}s"
    print(f"\n500-row load test: {elapsed:.2f}s, {rep['hard_errors']} hard errors reported")


def test_row_count_limit_enforced():
    rows = [
        {"item_id": f"MENU-{i:04d}", "category": "Breads", "item_name": f"Dish {i}",
         "price": 1.0, "dietary_type": "Veg", "image_filename": "x.jpg"}
        for i in range(501)
    ]
    files = {"file": ("too_big.xlsx", make_menu_xlsx(rows), "application/octet-stream")}
    resp = client.post(f"/tenants/{TENANT}/menu/upload", files=files)
    assert resp.status_code == 400
    assert "limit is 500" in resp.json()["error"]["message"]
