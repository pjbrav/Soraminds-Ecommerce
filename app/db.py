"""SQLite access layer.

One file (data/menu.db) holds every tenant's data at prototype scale.
See ARCHITECTURE.md for why SQLite over Postgres here, and what the
production swap looks like.
"""
from __future__ import annotations

import json
import sqlite3
import threading

from . import settings

_LOCK = threading.Lock()
_CONN: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
  id           TEXT PRIMARY KEY,
  tenant_id    TEXT NOT NULL,
  filename     TEXT NOT NULL,
  status       TEXT NOT NULL,             -- processing|ready|failed
  mapping_json TEXT,                      -- persisted column mapping (audit)
  notes        TEXT,
  created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS upload_items (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  upload_id    TEXT NOT NULL,
  tenant_id    TEXT NOT NULL,
  row_index    INTEGER NOT NULL,          -- 1-based sheet row the item came from
  item_id      TEXT NOT NULL,
  fields_json  TEXT NOT NULL,             -- normalized, post-repair field values
  status       TEXT NOT NULL,             -- valid | warning | error
  updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_upload_items_upload ON upload_items(upload_id, row_index);

CREATE TABLE IF NOT EXISTS validation_issues (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  upload_id   TEXT NOT NULL,
  item_row    INTEGER,                    -- NULL => upload-level issue
  field       TEXT,
  code        TEXT NOT NULL,
  severity    TEXT NOT NULL,              -- hard | soft | warning | info
  message     TEXT NOT NULL,
  suggestion  TEXT,
  extra_json  TEXT,                       -- machine-readable detail (e.g. options)
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_issues_upload ON validation_issues(upload_id);

CREATE TABLE IF NOT EXISTS photos (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  upload_id   TEXT NOT NULL,
  tenant_id   TEXT NOT NULL,
  filename    TEXT NOT NULL,
  matched_row INTEGER,                    -- row_index of the item referencing it
  ocr_json    TEXT,                       -- extracted {item_name, price, raw}
  created_at  TEXT NOT NULL,
  UNIQUE(upload_id, filename)
);

CREATE TABLE IF NOT EXISTS menu_versions (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id    TEXT NOT NULL,
  version_no   INTEGER NOT NULL,
  upload_id    TEXT,
  published_by TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  is_active    INTEGER NOT NULL DEFAULT 0,
  UNIQUE(tenant_id, version_no)
);

-- Published canonical records (the kiosk-ready JSON per item).
CREATE TABLE IF NOT EXISTS menu_items (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id    TEXT NOT NULL,
  version_no   INTEGER NOT NULL,
  item_id      TEXT NOT NULL,
  record_json  TEXT NOT NULL,
  UNIQUE(tenant_id, version_no, item_id)
);

CREATE TABLE IF NOT EXISTS tenant_state (
  tenant_id      TEXT PRIMARY KEY,
  active_version INTEGER
);

CREATE TABLE IF NOT EXISTS audit_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id   TEXT,
  actor       TEXT NOT NULL,
  action      TEXT NOT NULL,
  detail_json TEXT,
  created_at  TEXT NOT NULL
);

-- LLM accounting: one row per provider call (cost-aware usage pattern).
CREATE TABLE IF NOT EXISTS llm_usage (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  provider      TEXT NOT NULL,
  model         TEXT NOT NULL,
  purpose       TEXT NOT NULL,
  input_tokens  INTEGER,
  output_tokens INTEGER,
  item_ref      TEXT,
  created_at    TEXT NOT NULL
);

-- Tenant-scoped vector index (JSON-wrapped vectors; no vector DB at this scale).
CREATE TABLE IF NOT EXISTS embeddings (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id   TEXT NOT NULL,
  item_id     TEXT NOT NULL,
  model       TEXT NOT NULL,
  vector_id   TEXT NOT NULL,
  source_text TEXT NOT NULL,
  vector_json TEXT NOT NULL,
  version_no  INTEGER,
  UNIQUE(tenant_id, item_id, version_no)
);

CREATE TABLE IF NOT EXISTS jobs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id   TEXT NOT NULL,
  version_no  INTEGER,
  job_type    TEXT NOT NULL,              -- tts | image_resize | index_refresh
  status      TEXT NOT NULL,
  detail      TEXT,
  created_at  TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    """Return the process-wide connection, creating the DB on first use."""
    global _CONN
    with _LOCK:
        if _CONN is None:
            settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
            _CONN = sqlite3.connect(settings.DB_PATH, check_same_thread=False)
            _CONN.row_factory = sqlite3.Row
            _CONN.execute("PRAGMA journal_mode=WAL")
            _CONN.execute("PRAGMA foreign_keys=ON")
            _CONN.executescript(SCHEMA)
            _CONN.commit()
        return _CONN


def execute(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    conn = connect()
    cur = conn.execute(sql, params)
    conn.commit()
    return cur


def executemany(sql: str, seq: list[tuple]) -> None:
    """Batched writes in ONE transaction (critical for the 500-row load test)."""
    if not seq:
        return
    conn = connect()
    conn.executemany(sql, seq)
    conn.commit()


def query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return connect().execute(sql, params).fetchall()


def query_one(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    return connect().execute(sql, params).fetchone()


def reset_for_tests() -> None:
    """Close and recreate the connection (used by the test suite / demo runs)."""
    global _CONN
    with _LOCK:
        if _CONN is not None:
            _CONN.close()
            _CONN = None


def j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def unj(text: str):
    return json.loads(text) if text else None
