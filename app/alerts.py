"""alerts.notify: one alert row per user (guide 12.7)."""
from __future__ import annotations

from typing import Iterable

from app import db
from app.models.enums import AlertKind


def notify(user_ids: Iterable, kind: AlertKind | str, message: str, link: str, series_id=None, conn=None) -> int:
    kind = AlertKind(kind)  # rejects unknown kinds
    ids = list(dict.fromkeys(user_ids))
    sql = "insert into alerts (user_id, series_id, kind, message, link) values (%s, %s, %s, %s, %s)"
    rows = [(uid, series_id, kind.value, message, link) for uid in ids]
    if not rows:
        return 0
    if conn is not None:
        conn.cursor().executemany(sql, rows)
    else:
        with db.transaction() as c:
            c.cursor().executemany(sql, rows)
    return len(rows)


def admin_ids(conn) -> list:
    return [r["id"] for r in conn.execute("select id from users where role = 'admin' and status = 'active'").fetchall()]
