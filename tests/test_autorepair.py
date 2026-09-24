"""Auto-Repair transformations (spec §6)."""
from __future__ import annotations

from app.ingest.autorepair import apply_defaults_and_sort, auto_repair_row, normalize_price


def test_price_strips_currency_symbol():
    assert normalize_price("$13.99") == (13.99, "stripped currency symbol/whitespace")


def test_price_handles_decimal_comma():
    # The classic European typo: "$14,99" means 14.99
    assert normalize_price("$14,99")[0] == 14.99


def test_price_handles_thousands_separator():
    assert normalize_price("1,299.99")[0] == 1299.99


def test_price_plain_number_untouched():
    assert normalize_price("13.99") == (13.99, None)
    assert normalize_price(13.99) == (13.99, None)


def test_price_garbage_returns_none():
    assert normalize_price("expensive")[0] is None


def test_trim_whitespace_and_dropdown_casing(tenant, base_item):
    row = {
        "item_name": "  Garlic Naan  ",
        "category": " Breads ",
        "dietary_type": "non-veg",
        "spice_level": "MEDIUM",
        "price": " 2.49 ",
    }
    item = auto_repair_row(row, tenant)
    assert item["item_name"] == "Garlic Naan"
    assert item["category"] == "Breads"
    assert item["dietary_type"] == "Non-Veg"      # casing normalized
    assert item["spice_level"] == "Medium"
    assert item["price"] == 2.49
    assert any("dietary_type" in r for r in item["repairs"])


def test_html_tags_stripped_from_text(tenant):
    item = auto_repair_row({"item_name": "Naan <script>x</script>", "price": "1.99"}, tenant)
    assert "<" not in item["item_name"] and "script" not in item["item_name"].lower()
    assert item["item_name"].startswith("Naan")
    assert any("tags" in r for r in item["repairs"])


def test_documented_defaults_applied(tenant):
    items = [auto_repair_row({"item_name": "Roti", "price": "2.49"}, tenant)]
    apply_defaults_and_sort(items, tenant)
    item = items[0]
    assert item["spice_level"] == "None"
    assert item["is_available"] is True
    assert item["is_featured"] is False


def test_blank_sort_order_sequential_within_category(tenant):
    rows = [
        {"item_name": "A", "category": "Breads", "price": "1"},
        {"item_name": "B", "category": "Breads", "price": "1"},
        {"item_name": "C", "category": "Rice", "price": "1"},
        {"item_name": "D", "category": "Breads", "price": "1"},
    ]
    items = [auto_repair_row(r, tenant) for r in rows]
    apply_defaults_and_sort(items, tenant)
    assert [i["sort_order"] for i in items] == [1, 2, 1, 3]


def test_explicit_sort_order_preserved(tenant):
    items = [auto_repair_row({"item_name": "A", "price": "1", "sort_order": 7}, tenant)]
    apply_defaults_and_sort(items, tenant)
    assert items[0]["sort_order"] == 7


def test_allergens_and_tags_split_to_lists(tenant):
    item = auto_repair_row(
        {"item_name": "A", "price": "1", "allergens": "Dairy, Gluten", "tags": "spicy, fried"},
        tenant,
    )
    assert item["allergens"] == ["Dairy", "Gluten"]
    assert item["tags"] == ["spicy", "fried"]


def test_yes_no_parsing(tenant):
    item = auto_repair_row(
        {"item_name": "A", "price": "1", "is_featured": "YES", "is_available": "no"}, tenant
    )
    assert item["is_featured"] is True
    assert item["is_available"] is False


def test_template_description_fallback_flags_auto_generated(tenant):
    """Without an API key, blank descriptions get a deterministic fallback."""
    from app.ingest.llm import llm_service

    item = {"item_name": "Egg Masala", "category": "Non Veg Curries",
            "dietary_type": "Egg", "tags": ["comfort food"]}
    desc, method = llm_service.generate_description(item, tenant)
    assert method == "template-fallback"
    assert "Egg Masala" in desc and len(desc) <= 200
