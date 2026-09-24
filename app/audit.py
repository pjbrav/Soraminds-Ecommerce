"""Audit log: every mutating action (upload, auto-repair, owner edit, publish)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from . import db


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def audit(tenant_id: str | None, actor: str, action: str, **detail) -> None:
    db.execute(
        "INSERT INTO audit_log (tenant_id, actor, action, detail_json, created_at)"
        " VALUES (?,?,?,?,?)",
        (tenant_id, actor, action, json.dumps(detail, ensure_ascii=False, default=str), utcnow()),
    )


def recent(tenant_id: str | None = None, limit: int = 100) -> list[dict]:
    if tenant_id:
        rows = db.query(
            "SELECT * FROM audit_log WHERE tenant_id=? ORDER BY id DESC LIMIT ?",
            (tenant_id, limit),
        )
    else:
        rows = db.query("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]
