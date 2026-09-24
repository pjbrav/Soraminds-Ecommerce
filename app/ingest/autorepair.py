"""Auto-Repair pass (Step 4): deterministic fixes applied before the owner sees anything.

Trim whitespace; strip currency symbols/commas from price; normalize dropdown
casing; apply documented defaults; assign sequential sort_order within
category. Every repair is recorded as an audit note on the row.
"""
from __future__ import annotations

import re

from ..tenant_config import TenantConfig

TAG_RE = re.compile(r"<[^>]*>", re.IGNORECASE)

DEFAULTS = {
    "spice_level": "None",
    "is_available": "Yes",
    "is_featured": "No",
}


def normalize_price(raw) -> tuple[float | None, str | None]:
    """Parse a price string into a float, tolerating common owner formats.

    Returns (value, repair_note). Handles:
      "$13.99" -> 13.99        "1,299.99" -> 1299.99
      "$14,99" -> 14.99        " 12 "     -> 12.0
    """
    if raw is None or raw == "":
        return None, None
    if isinstance(raw, (int, float)):
        return float(raw), None
    text = str(raw).strip()
    note_parts: list[str] = []
    if text != str(text).strip() or "$" in text or "₹" in text or "€" in text:
        note_parts.append("stripped currency symbol/whitespace")
    cleaned = text.replace("$", "").replace("₹", "").replace("€", "").replace(" ", "")
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(",", "")          # 1,299.99 -> 1299.99
        note_parts.append("removed thousands separator")
    elif "," in cleaned:
        head, _, tail = cleaned.rpartition(",")
        if len(tail) <= 2 and "." not in cleaned:    # 14,99 -> 14.99 (decimal comma)
            cleaned = f"{head}.{tail}"
            note_parts.append("converted decimal comma to point")
        else:
            cleaned = cleaned.replace(",", "")
            note_parts.append("removed commas")
    try:
        value = float(cleaned)
    except ValueError:
        return None, None
    return value, ("; ".join(note_parts) if note_parts else None)


def _canonical_casing(value: str, options: list[str]) -> str | None:
    low = value.strip().lower()
    for option in options:
        if option.lower() == low:
            return option
    return None


def auto_repair_row(row: dict, tenant: TenantConfig) -> dict:
    """Apply all deterministic repairs to one raw row; returns repaired fields.

    The input dict maps canonical column -> raw string/None (post column
    mapping). The output is the normalized field dict used downstream.
    """
    repairs: list[str] = []

    def text(field: str) -> str:
        raw = row.get(field)
        value = (str(raw).strip() if raw is not None else "")
        return value

    item = {
        "item_id": text("item_id") or None,
        "category": text("category") or None,
        "subcategory": text("subcategory") or "",
        "item_name": text("item_name") or None,
        "description": text("description") or None,
        "description_auto_generated": False,
        "price_raw": row.get("price"),
        "price": None,
        "dietary_type": text("dietary_type") or None,
        "spice_level": text("spice_level") or None,
        "calorie_range": text("calorie_range") or None,
        "allergens_raw": text("allergens") or None,
        "image_filename": text("image_filename") or None,
        "is_featured_raw": text("is_featured") or None,
        "is_available_raw": text("is_available") or None,
        "sort_order_raw": row.get("sort_order"),
        "tags_raw": text("tags") or None,
        "repairs": repairs,
    }

    # Price: strip currency symbols / separators.
    price, note = normalize_price(row.get("price"))
    item["price"] = price
    if note:
        repairs.append(f"price: {note}")

    # Dropdown casing normalization ("non-veg" -> "Non-Veg").
    for field, options in (
        ("dietary_type", tenant.dietary_types),
        ("spice_level", tenant.spice_levels),
    ):
        raw = item[field]
        if raw:
            fixed = _canonical_casing(raw, options)
            if fixed and fixed != raw:
                item[field] = fixed
                repairs.append(f"{field}: normalized casing '{raw}' -> '{fixed}'")

    # Yes/No booleans.
    for field in ("is_featured", "is_available"):
        raw = item[f"{field}_raw"]
        if raw is None or raw == "":
            continue
        low = raw.strip().lower()
        if low in ("yes", "y", "true", "1"):
            item[field] = True
        elif low in ("no", "n", "false", "0"):
            item[field] = False
        else:
            item[field] = None

    # HTML/script tag stripping in free text (safety net; validation flags it too).
    for field in ("item_name", "description"):
        raw = item[field]
        if raw and TAG_RE.search(raw):
            item[field] = TAG_RE.sub("", raw).strip()
            repairs.append(f"{field}: removed HTML/script tags")

    # Allergens / tags: comma-separated -> list.
    item["allergens"] = (
        [a.strip() for a in item["allergens_raw"].split(",") if a.strip()]
        if item["allergens_raw"]
        else []
    )
    item["tags"] = (
        [t.strip() for t in item["tags_raw"].split(",") if t.strip()] if item["tags_raw"] else []
    )

    # sort_order: parse integer if present.
    sort_raw = row.get("sort_order")
    if sort_raw is None or str(sort_raw).strip() == "":
        item["sort_order"] = None
    else:
        try:
            item["sort_order"] = int(float(str(sort_raw).strip()))
        except ValueError:
            item["sort_order"] = None

    return item


def apply_defaults_and_sort(items: list[dict], tenant: TenantConfig) -> None:
    """Post-pass: documented defaults + sequential sort_order within category."""
    counters: dict[str, int] = {}
    for item in items:
        if item["spice_level"] is None:
            item["spice_level"] = DEFAULTS["spice_level"]
            item["repairs"].append("spice_level: blank -> default 'None'")
        if item.get("is_available") is None:
            item["is_available"] = True
            item["repairs"].append("is_available: blank -> default 'Yes'")
        if item.get("is_featured") is None:
            item["is_featured"] = False
            item["repairs"].append("is_featured: blank -> default 'No'")
        category = item.get("category") or ""
        counters[category] = counters.get(category, 0) + 1
        if item["sort_order"] is None:
            item["sort_order"] = counters[category]
            item["repairs"].append(f"sort_order: blank -> sequential within '{category or '?'}'")
