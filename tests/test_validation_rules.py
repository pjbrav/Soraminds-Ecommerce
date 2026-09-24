"""One test per validation rule in the spec's rule table (§5 Step 3)."""
from __future__ import annotations

from app.ingest.rules import (
    check_allergens,
    check_calorie_range,
    check_category,
    check_description,
    check_dietary_type,
    check_image_filename,
    check_item_name,
    check_price,
    check_sort_order,
    check_spice_level,
    row_status,
    validate_menu_level,
    validate_row,
)


def codes(issues):
    return [i["code"] for i in issues]


# ---- item_name: non-empty, 1-60 chars, no HTML/script tags ---------------

def test_item_name_empty_is_hard_error(base_item, ctx):
    base_item["item_name"] = ""
    issues = check_item_name(base_item, ctx)
    assert codes(issues) == ["item_name"] and issues[0]["severity"] == "hard"


def test_item_name_too_long_is_hard_error(base_item, ctx):
    base_item["item_name"] = "x" * 61
    issues = check_item_name(base_item, ctx)
    assert issues[0]["severity"] == "hard" and "60" in issues[0]["message"]


def test_item_name_with_script_tag_is_hard_error(base_item, ctx):
    base_item["item_name"] = "Naan <script>alert(1)</script>"
    issues = check_item_name(base_item, ctx)
    assert issues[0]["severity"] == "hard" and "HTML" in issues[0]["message"]


def test_item_name_valid_passes(base_item, ctx):
    assert check_item_name(base_item, ctx) == []


# ---- category: exact match or >= 0.85 fuzzy ------------------------------

def test_category_exact_match_passes(base_item, ctx):
    assert check_category(base_item, ctx) == []


def test_category_unknown_is_hard_error_with_suggestion(base_item, ctx):
    base_item["category"] = "Zzz Broken"
    issues = check_category(base_item, ctx)
    assert issues[0]["severity"] == "hard"
    assert "not recognized" in issues[0]["message"]
    assert issues[0]["suggestion"]


def test_category_missing_is_hard_error(base_item, ctx):
    base_item["category"] = None
    assert check_category(base_item, ctx)[0]["severity"] == "hard"


def test_category_unambiguous_fuzzy_match_auto_maps_with_flag(base_item, ctx):
    """A fuzzy match above threshold with a clear winner auto-maps, flagged."""
    base_item["category"] = "Dessert"  # clearly closest to Desserts
    issues = check_category(base_item, ctx)
    assert codes(issues) == ["category_auto_mapped"]
    assert issues[0]["severity"] == "soft"  # visible, never blocking
    assert base_item["category"] == "Desserts"


# ---- price: number > 0, <= 2 decimals, < 500 ------------------------------

def test_price_missing_is_hard_error(base_item, ctx):
    base_item["price"] = None
    assert check_price(base_item, ctx)[0]["severity"] == "hard"


def test_price_zero_is_hard_error(base_item, ctx):
    base_item["price"] = 0
    assert check_price(base_item, ctx)[0]["severity"] == "hard"


def test_price_negative_is_hard_error(base_item, ctx):
    base_item["price"] = -5
    assert check_price(base_item, ctx)[0]["severity"] == "hard"


def test_price_three_decimals_is_hard_error(base_item, ctx):
    base_item["price"] = 13.999
    issues = check_price(base_item, ctx)
    assert issues[0]["severity"] == "hard" and "decimal" in issues[0]["message"]


def test_price_above_ceiling_is_hard_error(base_item, ctx):
    base_item["price"] = 600
    issues = check_price(base_item, ctx)
    assert issues[0]["severity"] == "hard" and "500" in issues[0]["message"]


def test_price_valid_passes(base_item, ctx):
    assert check_price(base_item, ctx) == []


# ---- dietary_type: one of Veg / Non-Veg / Egg / Vegan ---------------------

def test_dietary_type_invalid_is_hard_error(base_item, ctx):
    base_item["dietary_type"] = "Meaty"
    assert check_dietary_type(base_item, ctx)[0]["severity"] == "hard"


def test_dietary_type_missing_is_hard_error(base_item, ctx):
    base_item["dietary_type"] = None
    assert check_dietary_type(base_item, ctx)[0]["severity"] == "hard"


# ---- spice_level: fixed list if present -----------------------------------

def test_spice_level_invalid_is_hard_error(base_item, ctx):
    base_item["spice_level"] = "Volcanic"
    assert check_spice_level(base_item, ctx)[0]["severity"] == "hard"


def test_spice_level_valid_passes(base_item, ctx):
    assert check_spice_level(base_item, ctx) == []


# ---- calorie_range: regex ^\d{2,4}-\d{2,4}$ -------------------------------

def test_calorie_range_malformed_is_soft_error(base_item, ctx):
    base_item["calorie_range"] = "550-7"
    issues = check_calorie_range(base_item, ctx)
    assert issues[0]["severity"] == "soft" and "NNN-NNN" in issues[0]["message"]


def test_calorie_range_valid_passes(base_item, ctx):
    assert check_calorie_range(base_item, ctx) == []


# ---- allergens: each value in the known list ------------------------------

def test_allergen_unknown_is_soft_error_with_suggestion(base_item, ctx):
    base_item["allergens"] = ["Gluten", "Spices"]
    issues = check_allergens(base_item, ctx)
    assert issues[0]["severity"] == "soft"
    assert "Spices" in issues[0]["message"] and issues[0]["suggestion"]


def test_allergens_valid_passes(base_item, ctx):
    assert check_allergens(base_item, ctx) == []


# ---- description: <= 200 chars -------------------------------------------

def test_description_too_long_is_soft_error(base_item, ctx):
    base_item["description"] = "y" * 201
    issues = check_description(base_item, ctx)
    assert issues[0]["severity"] == "soft"


def test_description_blank_is_auto_fixed_upstream(base_item, ctx):
    base_item["description"] = None
    assert check_description(base_item, ctx) == []


# ---- image_filename: matches an uploaded file (case-insensitive) ----------

def test_image_missing_file_is_hard_error(base_item, ctx):
    base_item["image_filename"] = "not_uploaded.jpg"
    issues = check_image_filename(base_item, ctx)
    assert issues[0]["severity"] == "hard" and "No uploaded photo" in issues[0]["message"]


def test_image_case_insensitive_match_passes_with_info(base_item, ctx):
    base_item["image_filename"] = "GOAT_BIRYANI.JPG"
    issues = check_image_filename(base_item, ctx)
    assert codes(issues) == ["image_case_insensitive_match"]
    assert issues[0]["severity"] == "info"


def test_image_missing_value_is_hard_error(base_item, ctx):
    base_item["image_filename"] = None
    assert check_image_filename(base_item, ctx)[0]["severity"] == "hard"


# ---- sort_order: integer if present ---------------------------------------

def test_sort_order_non_integer_is_soft_error(base_item, ctx):
    base_item["sort_order_invalid"] = True
    base_item["sort_order_raw"] = "third"
    issues = check_sort_order(base_item, ctx)
    assert issues[0]["severity"] == "soft"


# ---- is_featured count and near-duplicates: warning only ------------------

def test_too_many_featured_is_warning_never_blocks(tenant, base_item):
    items = []
    for i in range(12):
        row = dict(base_item, item_id=f"M{i}", is_featured=True)
        items.append(row)
    issues = validate_menu_level(items, tenant)
    assert any(i[1]["code"] == "too_many_featured" and i[1]["severity"] == "warning" for i in issues)


def test_near_duplicate_detection_is_warning(tenant, base_item):
    items = [dict(base_item, item_name="Paneer Biryani"), dict(base_item, item_name="Paneer Biriyani")]
    issues = validate_menu_level(items, tenant)
    assert any(i[1]["code"] == "near_duplicate" for i in issues)


# ---- row status aggregation ------------------------------------------------

def test_row_status_error_beats_warning_beats_valid():
    assert row_status([{"severity": "hard"}]) == "error"
    assert row_status([{"severity": "soft"}]) == "warning"
    assert row_status([{"severity": "info"}]) == "valid"
    assert row_status([{"severity": "soft"}, {"severity": "hard"}]) == "error"


def test_validate_row_collects_every_error_not_just_the_first(base_item, ctx):
    base_item.update({"item_name": "", "price": None, "dietary_type": "Wrong", "category": "Nope"})
    issues = validate_row(base_item, ctx)
    # four distinct hard failures must all be present in one pass
    assert len([i for i in issues if i["severity"] == "hard"]) >= 4
