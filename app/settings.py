"""App-wide settings (env-overridable, no secrets ever hardcoded)."""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Root for the local filesystem standing in for S3 object storage.
STORAGE_ROOT = Path(os.environ.get("SPICEHUB_STORAGE_ROOT", REPO_ROOT / "storage"))

# SQLite database file (one file holds every tenant's data at prototype scale).
DATA_DIR = Path(os.environ.get("SPICEHUB_DATA_DIR", REPO_ROOT / "data"))
DB_PATH = DATA_DIR / "menu.db"

# Tenant configuration lives in config/tenants/<tenant_id>.yaml
TENANT_CONFIG_DIR = REPO_ROOT / "config" / "tenants"

# LLM / vision configuration. Never hardcode keys; read from the environment.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
OPENAI_CHAT_MODEL = os.environ.get("OPENAI_CHAT_MODEL", "gpt-4o-mini")
OPENAI_VISION_MODEL = os.environ.get("OPENAI_VISION_MODEL", "gpt-4o-mini")

# Embedding provider: "auto" (sentence-transformers if installed, hashed-trigram
# fallback otherwise), "local" (force sentence-transformers), or "fallback".
EMBEDDING_PROVIDER = os.environ.get("SPICEHUB_EMBEDDING_PROVIDER", "auto")
EMBEDDING_MODEL_NAME = os.environ.get("SPICEHUB_EMBEDDING_MODEL", "all-MiniLM-L6-v2")


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STORAGE_ROOT.mkdir(parents=True, exist_ok=True)
