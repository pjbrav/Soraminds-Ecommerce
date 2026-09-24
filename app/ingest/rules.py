"""Field validation (Step 3): a RULE TABLE, not per-field hardcoded logic.

Every rule runs against every row and EVERY failure is collected — the
validator never stops at the first error. Thresholds and option lists come
from the tenant's YAML config, never from constants here.

Severity semantics (spec §5 Step 6):
  hard     -> row status "Error", blocks publish until fixed
  soft     -> row status "Warning", never blocks
  info     -> visible note on the row (e.g. case-insensitive photo match)
  warning  -> menu-level warnings (duplicates, too many featured)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from rapidfuzz import fuzz

from ..tenant_config import TenantConfig

Issue = dict  # {field, code, severity, message, suggestion, extra}

CALORIE_RE = re.compile(r"^\d{2,4}-\d{2,4}$")
TAG_RE = re.compile(r"<[^>]*>", re.IGNORECASE)

HARD, SOFT, INFO, WARN = "hard", "soft", "info", "warning"


@dataclass
class RuleContext:
    tenant: TenantConfig
    photo_filenames: set[str] = field(default_factory=set)  # lowercase -> actual name
    similarity: Callable[[str, str], float] | None = None


@dataclass
class Rule:
    """One row in the validation rule table."""

    field: str
    code: str
    severity: str
    check: Callable[[dict, RuleContext], list[Issue]]


def _issue(rule: Rule, message: str, suggestion: str | None = None, **extra) -> Issue:
    return {
        "field": rule.field,
        "code": rule.code,
        "severity": rule.severity,
        "message": message,
        "suggestion": suggestion,
        "extra": extra or None,
    }


# --------------------------------------------------------------------------
# Individual rule checks. Each returns a list (usually 0 or 1) of issues.
# --------------------------------------------------------------------------

def check_item_name(item: dict, ctx: RuleContext) -> list[Issue]:
    rule = Rule("item_name", "item_name", HARD, check_item_name)
    name = item.get("item_name")
    if not name:
        return [
            _issue(rule, "Item name is empty.", "Type the dish name for this row — it is required.")
        ]
    if len(name) > 60:
        return [
            _issue(
                rule,
                f"Item name is {len(name)} characters; the maximum is 60.",
                "Shorten the name (move details into the description).",
            )
        ]
    if TAG_RE.search(name):
        return [
            _issue(
                rule,
                "Item name contains HTML or script tags.",
                "Remove the tags — only plain text is allowed.",
            )
        ]
    return []


def check_category(item: dict, ctx: RuleContext) -> list[Issue]:
    """Exact match, or >= 0.85 fuzzy match (auto-mapped and flagged).

    When two categories score within the ambiguity gap of each other
    ("Currys" -> Veg Currys vs Non Veg Currys), the owner is asked to pick
    instead of the system silently mis-filing the item.
    """
    rule = Rule("category", "category", HARD, check_category)
    tenant = ctx.tenant
    raw = item.get("category")
    if not raw:
        return [
            _issue(
                rule,
                "Category is empty.",
                f"Pick one of: {', '.join(tenant.categories)}.",
            )
        ]
    if raw in tenant.category_set:
        return []

    threshold = tenant.threshold("fuzzy_match", 0.85)
    gap = tenant.threshold("ambiguity_gap", 0.05)
    if ctx.similarity is not None:
        scored = sorted(
            ((ctx.similarity(raw, c), c) for c in tenant.categories), reverse=True
        )
    else:  # text fallback when no embedding provider is available at all
        scored = sorted(
            ((fuzz.token_sort_ratio(raw, c) / 100.0, c) for c in tenant.categories),
            reverse=True,
        )
    if not scored:
        return []

    top_score, top_cat = scored[0]
    second_score, second_cat = scored[1] if len(scored) > 1 else (0.0, "")

    if top_score >= threshold and (top_score - second_score) > gap:
        # Unambiguous fuzzy match: auto-map, but flag it for owner review.
        item["category"] = top_cat
        item["repairs"].append(f"category: fuzzy-mapped '{raw}' -> '{top_cat}'")
        return [
            {
                "field": "category",
                "code": "category_auto_mapped",
                "severity": SOFT,
                "message": f"Category '{raw}' was auto-matched to '{top_cat}' "
                f"(similarity {top_score:.2f}).",
                "suggestion": "Confirm this is the right category, or pick another one.",
                "extra": {"options": tenant.categories, "matched": top_cat},
            }
        ]
    if top_score >= threshold:
        return [
            _issue(
                rule,
                f"Category '{raw}' is ambiguous — it matches '{top_cat}' "
                f"({top_score:.2f}) and '{second_cat}' ({second_score:.2f}) equally well.",
                f"Did you mean '{top_cat}' or '{second_cat}'? Pick the correct one.",
                options=tenant.categories,
            )
        ]
    suggestions = ", ".join(f"'{c}'" for c, in [(c,) for _, c in scored[:3]])
    return [
        _issue(
            rule,
            f"Category '{raw}' is not recognized.",
            f"Did you mean {suggestions}? Pick the closest match from the list.",
            options=tenant.categories,
        )
    ]


def check_price(item: dict, ctx: RuleContext) -> list[Issue]:
    rule = Rule("price", "price", HARD, check_price)
    price = item.get("price")
    if price is None:
        return [
            _issue(
                rule,
                "Price is missing or could not be read as a number.",
                "Enter the price as a plain number, e.g. 13.99 (no $ sign).",
            )
        ]
    if price <= 0:
        return [_issue(rule, f"Price must be greater than 0 (got {price}).", "Enter a positive price.")]
    if abs(round(price, 2) - price) > 1e-9:
        return [
            _issue(
                rule,
                f"Price {price} has more than 2 decimal places.",
                "Round to at most 2 decimals, e.g. 13.99.",
            )
        ]
    if price >= 500:
        return [
            _issue(
                rule,
                f"Price {price} is above the $500 sanity ceiling.",
                "This looks like a typo — please confirm the real price.",
            )
        ]
    return []


def check_dietary_type(item: dict, ctx: RuleContext) -> list[Issue]:
    rule = Rule("dietary_type", "dietary_type", HARD, check_dietary_type)
    value = item.get("dietary_type")
    options = ctx.tenant.dietary_types
    if not value:
        return [
            _issue(rule, "Dietary type is empty.", f"Pick one of: {', '.join(options)}.", options=options)
        ]
    if value not in options:
        return [
            _issue(
                rule,
                f"Dietary type '{value}' is not one of: {', '.join(options)}.",
                "Pick the closest option from the dropdown.",
                options=options,
            )
        ]
    return []


def check_spice_level(item: dict, ctx: RuleContext) -> list[Issue]:
    rule = Rule("spice_level", "spice_level", HARD, check_spice_level)
    value = item.get("spice_level")
    options = ctx.tenant.spice_levels
    if value and value not in options:
        return [
            _issue(
                rule,
                f"Spice level '{value}' is not one of: {', '.join(options)}.",
                "Pick the closest option from the dropdown.",
                options=options,
            )
        ]
    return []


def check_calorie_range(item: dict, ctx: RuleContext) -> list[Issue]:
    rule = Rule("calorie_range", "calorie_range", SOFT, check_calorie_range)
    value = item.get("calorie_range")
    if value and not CALORIE_RE.match(value):
        return [
            _issue(
                rule,
                f"Calorie range '{value}' is not in the NNN-NNN format.",
                "Use a range like 350-500 (or clear the field to skip it).",
            )
        ]
    return []


def check_allergens(item: dict, ctx: RuleContext) -> list[Issue]:
    rule = Rule("allergens", "allergens", SOFT, check_allergens)
    known = ctx.tenant.allergen_set
    unknown = [a for a in item.get("allergens") or [] if a not in known]
    if unknown:
        suggestions = []
        for a in unknown:
            best = max(ctx.tenant.allergens, key=lambda k: fuzz.ratio(a.lower(), k.lower()))
            suggestions.append(f"{a} -> {best}")
        return [
            _issue(
                rule,
                f"Unrecognized allergen(s): {', '.join(unknown)}.",
                "Known allergens: "
                + ", ".join(ctx.tenant.allergens)
                + ". Did you mean: "
                + "; ".join(suggestions)
                + "?",
            )
        ]
    return []


def check_description(item: dict, ctx: RuleContext) -> list[Issue]:
    rule = Rule("description", "description", SOFT, check_description)
    desc = item.get("description")
    if desc and len(desc) > 200:
        return [
            _issue(
                rule,
                f"Description is {len(desc)} characters; the maximum is 200.",
                "Shorten it — the kiosk truncates longer text.",
            )
        ]
    return []


def check_image_filename(item: dict, ctx: RuleContext) -> list[Issue]:
    rule = Rule("image_filename", "image_filename", HARD, check_image_filename)
    value = item.get("image_filename")
    if not value:
        if item.get("owner_added"):
            # Rows created from the dashboard have no photo file to reference
            # yet; they can publish with the kiosk's neutral placeholder.
            soft_rule = Rule("image_filename", "image_filename", WARN, check_image_filename)
            return [
                _issue(
                    soft_rule,
                    "No photo for this item yet.",
                    "It will show a neutral placeholder on the kiosk. Assign "
                    "one of the unmatched photos above if it belongs here.",
                )
            ]
        return [
            _issue(
                rule,
                "No photo filename given for this item.",
                "Type the file name of the matching photo, or upload the photo.",
            )
        ]
    actual = ctx.photo_filenames.get(str(value).strip().lower())
    if actual is None:
        return [
            _issue(
                rule,
                f"No uploaded photo matches '{value}'.",
                "Check the spelling of the file name, or upload the missing photo.",
            )
        ]
    if actual != str(value).strip():
        return [
            {
                "field": "image_filename",
                "code": "image_case_insensitive_match",
                "severity": INFO,
                "message": f"Photo '{value}' matched uploaded file '{actual}' "
                "(file names are not case-sensitive).",
                "suggestion": None,
                "extra": None,
            }
        ]
    return []


def check_sort_order(item: dict, ctx: RuleContext) -> list[Issue]:
    rule = Rule("sort_order", "sort_order", SOFT, check_sort_order)
    if item.get("sort_order_invalid"):
        return [
            _issue(
                rule,
                f"Sort order '{item.get('sort_order_raw')}' is not a whole number.",
                "Enter a number like 3, or clear the cell to auto-assign by row order.",
            )
        ]
    return []


# The rule TABLE. Thresholds/limits inside checks come from tenant config.
RULE_TABLE: list[Rule] = [
    Rule("item_name", "item_name", HARD, check_item_name),
    Rule("category", "category", HARD, check_category),
    Rule("price", "price", HARD, check_price),
    Rule("dietary_type", "dietary_type", HARD, check_dietary_type),
    Rule("spice_level", "spice_level", HARD, check_spice_level),
    Rule("image_filename", "image_filename", HARD, check_image_filename),
    Rule("description", "description", SOFT, check_description),
    Rule("calorie_range", "calorie_range", SOFT, check_calorie_range),
    Rule("allergens", "allergens", SOFT, check_allergens),
    Rule("sort_order", "sort_order", SOFT, check_sort_order),
]


def validate_row(item: dict, ctx: RuleContext) -> list[Issue]:
    """Run every rule against one row; collect every issue."""
    issues: list[Issue] = []
    for rule in RULE_TABLE:
        issues.extend(rule.check(item, ctx))
    if item.get("description_auto_generated"):
        issues.append(
            {
                "field": "description",
                "code": "description_auto_generated",
                "severity": INFO,
                "message": "Description was generated automatically — skim it before publishing.",
                "suggestion": "Edit the text below if you want to change it.",
                "extra": None,
            }
        )
    return issues


def row_status(issues: list[Issue]) -> str:
    severities = {i["severity"] for i in issues}
    if HARD in severities:
        return "error"
    if SOFT in severities or WARN in severities:
        return "warning"
    return "valid"


def validate_menu_level(items: list[dict], tenant: TenantConfig) -> list[tuple[int | None, Issue]]:
    """Menu-wide warnings: featured-count and near-duplicate detection."""
    results: list[tuple[int | None, Issue]] = []
    limit = tenant.threshold("featured_warn_count", 10)
    featured = [i for i in items if i.get("is_featured")]
    if len(featured) > limit:
        results.append(
            (
                None,
                {
                    "field": "is_featured",
                    "code": "too_many_featured",
                    "severity": WARN,
                    "message": f"{len(featured)} items are marked featured (more than {limit}).",
                    "suggestion": "Featured items should be a small highlight list — "
                    "unflag some rows (this never blocks publishing).",
                    "extra": None,
                },
            )
        )
    # Near-duplicate detection on item_name within the same category.
    threshold = tenant.threshold("duplicate_similarity", 0.90)
    for a_idx, a in enumerate(items):
        for b_idx in range(a_idx + 1, len(items)):
            b = items[b_idx]
            if not a.get("item_name") or not b.get("item_name"):
                continue
            if (a.get("category") or "") != (b.get("category") or ""):
                continue
            # score_cutoff makes near-miss pairs short-circuit (load-test speed).
            sim = fuzz.token_sort_ratio(a["item_name"], b["item_name"],
                                         score_cutoff=int(threshold * 100)) / 100.0
            if sim >= threshold:
                msg = (
                    f"'{a['item_name']}' looks like a duplicate of "
                    f"'{b['item_name']}' in the same category."
                )
                issue = {
                    "field": "item_name",
                    "code": "near_duplicate",
                    "severity": WARN,
                    "message": msg,
                    "suggestion": "If these are two different dishes, rename one. "
                    "If not, use the Delete row button below the item to remove it.",
                    "extra": {"duplicate_of_row": b_idx + 1, "similarity": round(sim, 2)},
                }
                results.append((a_idx + 1, issue))
    return results
