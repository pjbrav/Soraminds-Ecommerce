"""Canonical kiosk-ready record builder (spec §7 JSON schema).

This JSON — not the Excel file — is the source of truth from publish onward.
"""
from __future__ import annotations

from .audit import utcnow
from .ingest.embeddings import embed, embedding_source_text, vector_id
from .tenant_config import TenantConfig

RUNTIME_STATS_NOTE = (
    "Populated by the Analytics/Feedback blocks after launch, never by menu upload."
)


def build_canonical_record(
    item: dict, tenant: TenantConfig, version_no: int, image_url: str
) -> dict:
    """Turn one validated item into the canonical RAG-ready record."""
    source_text = embedding_source_text(item)
    model, vector = embed(source_text)
    vid = vector_id(source_text + tenant.tenant_id)
    now = utcnow()
    return {
        "item_id": item["item_id"],
        "tenant_id": tenant.tenant_id,
        "category": item.get("category") or "",
        "subcategory": item.get("subcategory") or "",
        "item_name": item.get("item_name") or "",
        "description": item.get("description") or "",
        "description_auto_generated": bool(item.get("description_auto_generated")),
        "price": item.get("price"),
        "currency": tenant.currency,
        "dietary_type": item.get("dietary_type") or "",
        "spice_level": item.get("spice_level") or "None",
        "calorie_range": item.get("calorie_range") or "",
        "allergens": item.get("allergens") or [],
        "image": {
            "filename": item.get("image_filename") or "",
            "url": image_url,
            "status": "approved",
        },
        "is_featured": bool(item.get("is_featured")),
        "is_available": bool(item.get("is_available")),
        "sort_order": item.get("sort_order") or 0,
        "tags": item.get("tags") or [],
        "embedding": {
            "model": model,
            "vector_id": vid,
            "source_text": source_text,
        },
        "voice": {
            "audio_url": None,
            "status": "not_generated",  # TTS is a stubbed downstream job (see publish)
        },
        "runtime_stats": {
            "note": RUNTIME_STATS_NOTE,
            "popularity_rank": None,
            "like_percentage": None,
            "review_count": 0,
        },
        "menu_version": version_no,
        "created_at": now,
        "updated_at": now,
    }, {
        "model": model,
        "vector_id": vid,
        "source_text": source_text,
        "vector": vector,
    }
