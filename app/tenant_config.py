"""Tenant configuration loader (rule table / config, never hardcoded logic)."""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

import yaml

from . import settings
from .errors import TenantNotFoundError


@dataclass(frozen=True)
class TenantConfig:
    tenant_id: str
    restaurant_name: str
    city: str
    cuisine: str
    currency: str
    categories: list[str]
    dietary_types: list[str]
    spice_levels: list[str]
    allergens: list[str]
    limits: dict = field(default_factory=dict)
    thresholds: dict = field(default_factory=dict)
    embedding_model: str = "all-MiniLM-L6-v2"

    @property
    def category_set(self) -> set[str]:
        return set(self.categories)

    @property
    def allergen_set(self) -> set[str]:
        return set(self.allergens)

    def limit(self, key: str, default):
        return self.limits.get(key, default)

    def threshold(self, key: str, default):
        return self.thresholds.get(key, default)


def load_tenant(tenant_id: str) -> TenantConfig:
    path = settings.TENANT_CONFIG_DIR / f"{tenant_id}.yaml"
    if not path.exists():
        available = sorted(p.stem for p in settings.TENANT_CONFIG_DIR.glob("*.yaml"))
        raise TenantNotFoundError(
            f"Unknown tenant '{tenant_id}'. Available tenants: {available}"
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return TenantConfig(
        tenant_id=raw["tenant_id"],
        restaurant_name=raw.get("restaurant_name", tenant_id),
        city=raw.get("city", ""),
        cuisine=raw.get("cuisine", ""),
        currency=raw.get("currency", "USD"),
        categories=raw["categories"],
        dietary_types=raw.get("dietary_types", ["Veg", "Non-Veg", "Egg", "Vegan"]),
        spice_levels=raw.get("spice_levels", ["None", "Mild", "Medium", "Hot", "Extra Hot"]),
        allergens=raw.get("allergens", []),
        limits=raw.get("limits", {}),
        thresholds=raw.get("thresholds", {}),
        embedding_model=raw.get("embedding_model", "all-MiniLM-L6-v2"),
    )


@lru_cache(maxsize=8)
def get_tenant(tenant_id: str) -> TenantConfig:
    return load_tenant(tenant_id)


def available_tenants() -> list[str]:
    if not settings.TENANT_CONFIG_DIR.exists():
        return []
    return sorted(p.stem for p in settings.TENANT_CONFIG_DIR.glob("*.yaml"))
