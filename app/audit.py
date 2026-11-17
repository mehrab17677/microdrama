"""Audit log writer."""
from __future__ import annotations

from psycopg.types.json import Jsonb


def log(conn, user_id, action: str, series_id=None, detail: dict | None = None) -> None:
    conn.execute(
        "insert into audit_log (user_id, series_id, action, detail) values (%s, %s, %s, %s)",
        (user_id, series_id, action, Jsonb(detail or {})),
    )
