"""Append-only audit trail: who did what, when, before and after."""
from __future__ import annotations

import json

from . import db


def log(actor: str, action: str, entity: str, entity_id: str = "",
        before=None, after=None, version: str = "") -> None:
    def dump(v):
        if v is None:
            return None
        return v if isinstance(v, str) else json.dumps(v, default=str)

    db.execute(
        "INSERT INTO audit_log(ts, actor, action, entity, entity_id, before, after, version)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (db.now(), actor, action, entity, entity_id, dump(before), dump(after), version),
    )


def read(limit: int = 500):
    return db.query("SELECT * FROM audit_log ORDER BY log_id DESC LIMIT ?", (limit,))
