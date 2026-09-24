"""Column mapping (Step 2): exact match first, embedding fuzzy-match at 0.85."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..tenant_config import TenantConfig
from .sheet import CANONICAL_COLUMNS, REQUIRED_COLUMNS

# Reference phrases per canonical field: an owner renaming "item_name" to
# "Dish", "Item" or "Menu Item Name" still auto-maps via embedding similarity.
REFERENCE_PHRASES: dict[str, list[str]] = {
    "item_id": ["item id", "id", "sku", "item number"],
    "category": ["category", "menu category", "course", "section"],
    "subcategory": ["subcategory", "sub category", "sub-section"],
    "item_name": ["item name", "dish", "dish name", "item", "menu item", "name"],
    "description": ["description", "desc", "details", "about the dish"],
    "price": ["price", "cost", "amount", "usd", "price in dollars"],
    "dietary_type": ["dietary type", "diet type", "vegetarian type", "food type"],
    "spice_level": ["spice level", "spiciness", "heat level"],
    "calorie_range": ["calorie range", "calories", "calorie count", "kcal"],
    "allergens": ["allergens", "allergy info", "allergen info", "contains"],
    "image_filename": ["image filename", "image file", "photo name", "photo filename", "image"],
    "is_featured": ["is featured", "featured", "highlight", "is it featured"],
    "is_available": ["is available", "available", "in stock", "availability"],
    "sort_order": ["sort order", "order", "display order", "position"],
    "tags": ["tags", "keywords", "labels", "search tags"],
}


@dataclass
class MappingResult:
    """maps header text -> canonical column; issues describe unmapped columns."""

    mapping: dict[str, str] = field(default_factory=dict)
    issues: list[dict] = field(default_factory=list)
    auto_mapped: list[dict] = field(default_factory=list)

    def canonical(self, header: str) -> str | None:
        return self.mapping.get(header)


def build_mapping(
    headers: list[str], tenant: TenantConfig, similarity
) -> MappingResult:
    """Map raw headers to canonical columns.

    Exact match first (case-insensitive, trimmed); embedding cosine similarity
    against the reference phrases second; anything below the tenant's
    fuzzy-match threshold (0.85 per the spec) is flagged unmapped_column.
    """
    result = MappingResult()
    threshold = tenant.threshold("fuzzy_match", 0.85)
    used: set[str] = set()
    for header in headers:
        key = header.strip().lower()
        if key in CANONICAL_COLUMNS and key not in used:
            result.mapping[header] = key
            used.add(key)

    for header in headers:
        if header in result.mapping:
            continue
        scored: list[tuple[float, str]] = []
        for column, phrases in REFERENCE_PHRASES.items():
            if column in used:
                continue
            best = max(similarity(header, phrase) for phrase in phrases)
            scored.append((best, column))
        if not scored:
            continue
        best_score, best_column = max(scored)
        if best_score >= threshold:
            result.mapping[header] = best_column
            used.add(best_column)
            result.auto_mapped.append(
                {"header": header, "column": best_column, "similarity": round(best_score, 3)}
            )
        else:
            top = sorted(scored, reverse=True)[:3]
            suggestions = ", ".join(f"{c} ({s:.2f})" for s, c in top)
            result.issues.append(
                {
                    "field": None,
                    "code": "unmapped_column",
                    "severity": "hard",
                    "message": f"Column '{header}' could not be matched to a known menu column.",
                    "suggestion": (
                        f"Rename the column to match the template (closest guesses: "
                        f"{suggestions}). Mapping is automatic at >= {threshold} similarity."
                    ),
                }
            )

    for required in REQUIRED_COLUMNS:
        if required not in used:
            result.issues.append(
                {
                    "field": required,
                    "code": "missing_required_column",
                    "severity": "hard",
                    "message": f"Required column '{required}' is missing from the file.",
                    "suggestion": f"Add a '{required}' column and re-upload.",
                }
            )
    return result
