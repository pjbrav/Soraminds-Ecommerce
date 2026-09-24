"""Shared fixtures: isolated temp DB + storage, fast deterministic embeddings.

Env vars are set BEFORE any app import so the settings module picks them up.
"""
from __future__ import annotations

# Some CI sandboxes cap virtual memory below what a full test run needs.
import resource

try:
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    if soft != resource.RLIM_INFINITY and hard == resource.RLIM_INFINITY:
        resource.setrlimit(resource.RLIMIT_AS, (resource.RLIM_INFINITY, resource.RLIM_INFINITY))
except Exception:  # noqa: BLE001
    pass

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="spicehub-tests-")
os.environ["SPICEHUB_DATA_DIR"] = os.path.join(_TMP, "data")
os.environ["SPICEHUB_STORAGE_ROOT"] = os.path.join(_TMP, "storage")
os.environ["SPICEHUB_EMBEDDING_PROVIDER"] = "fallback"

import pytest  # noqa: E402

from app import db  # noqa: E402

TABLES = [
    "uploads", "upload_items", "validation_issues", "photos", "menu_versions",
    "menu_items", "tenant_state", "audit_log", "llm_usage", "embeddings", "jobs",
]


@pytest.fixture(autouse=True)
def clean_db():
    conn = db.connect()
    for table in TABLES:
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
    yield


@pytest.fixture()
def tenant():
    from app.tenant_config import get_tenant

    return get_tenant("spicehub-kitchen-troy")


@pytest.fixture()
def ctx(tenant):
    from app.ingest.rules import RuleContext

    photos = {
        "chicken_biryani.jpg": "chicken_biryani.jpg",
        "goat_biryani.jpg": "goat_biryani.jpg",
        "samosa.jpg": "samosa.jpg",
    }
    from app.ingest import embeddings

    return RuleContext(
        tenant=tenant, photo_filenames=photos, similarity=embeddings.similarity
    )


@pytest.fixture()
def base_item():
    """A fully valid row (post-repair shape)."""
    return {
        "item_id": "MENU-0001",
        "category": "Veg Curries",
        "subcategory": "",
        "item_name": "Paneer Butter Masala",
        "description": "Cubes of home-made indian cheese in a creamy curry sauce.",
        "description_auto_generated": False,
        "price": 13.99,
        "dietary_type": "Veg",
        "spice_level": "Mild",
        "calorie_range": "450-600",
        "allergens": ["Dairy"],
        "image_filename": "chicken_biryani.jpg",
        "is_featured": False,
        "is_available": True,
        "sort_order": 1,
        "tags": ["creamy"],
        "repairs": [],
    }
