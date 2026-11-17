"""Helpers shared by the graph nodes. Nodes read and write Postgres; they never call vendors."""
from __future__ import annotations

from psycopg.types.json import Jsonb

from app import alerts, audit, db
from app.models.enums import AlertKind

SERIES_COLUMNS = {"status", "current_stage", "batch", "waiting_group", "episode_count", "title", "discard_category", "discard_notes"}


def get_series(series_id) -> dict:
    row = db.fetch_one("select * from series where id = %s", (series_id,))
    if row is None:
        raise LookupError(f"series {series_id} not found")
    return row


def set_series(series_id, conn=None, **fields) -> None:
    bad = set(fields) - SERIES_COLUMNS
    if bad:
        raise ValueError(f"cannot set series columns {bad}")
    if not fields:
        return
    sets = ", ".join(f"{k} = %s" for k in fields)
    sql = f"update series set {sets}, updated_at = now() where id = %s"
    params = (*fields.values(), series_id)
    if conn is not None:
        conn.execute(sql, params)
    else:
        db.execute(sql, params)


def recipients(conn, series_id, with_admin: bool = True) -> list:
    owner = conn.execute("select owner_id from series where id = %s", (series_id,)).fetchone()["owner_id"]
    return [owner, *alerts.admin_ids(conn)] if with_admin else [owner]


def notify_series(series_id, kind: AlertKind, message: str, link: str | None = None, with_admin: bool = True) -> None:
    with db.transaction() as conn:
        alerts.notify(recipients(conn, series_id, with_admin), kind, message, link or f"/productions/{series_id}", series_id, conn=conn)


def fail_series(series_id, step: str, message: str) -> None:
    """Set the series to failed and alert the owner and admin (alert kind step_failed)."""
    with db.transaction() as conn:
        conn.execute("update series set status = 'failed', waiting_group = null, updated_at = now() where id = %s", (series_id,))
        audit.log(conn, None, "series.failed", series_id, {"step": step, "message": message})
        alerts.notify(recipients(conn, series_id), AlertKind.step_failed, f"Production stopped at {step}: {message}", f"/productions/{series_id}", series_id, conn=conn)


# ---- notes and verdicts: small facts the graph records in audit_log -------------------------

def add_note(series_id, key: str, text: str, **extra) -> None:
    with db.transaction() as conn:
        audit.log(conn, None, f"note:{key}", series_id, {"text": text, **extra})


def latest_note(series_id, key: str) -> str:
    row = db.fetch_one(
        "select detail from audit_log where series_id = %s and action = %s order by created_at desc, id desc limit 1", (series_id, f"note:{key}")
    )
    return row["detail"].get("text", "") if row else ""


def count_notes(series_id, key: str) -> int:
    return db.fetch_one("select count(*) n from audit_log where series_id = %s and action = %s", (series_id, f"note:{key}"))["n"]


def set_verdict(series_id, node: str, verdict: str) -> None:
    add_note(series_id, f"verdict:{node}", verdict)


def get_verdict(series_id, node: str) -> str:
    return latest_note(series_id, f"verdict:{node}")


def gate_revs(series_id, gate: int) -> int:
    """How many times this gate sent work back (rejections, feedback): requests carry it so redone work
    is new work. An approval does not count, so finished steps stay finished."""
    return db.fetch_one(
        "select count(*) n from gate_decisions where series_id = %s and gate = %s and decision not in ('approve','continue')", (series_id, gate)
    )["n"]


def latest_brief(series_id) -> dict:
    return db.fetch_one("select * from briefs where series_id = %s order by version desc limit 1", (series_id,))


def latest_bible(series_id) -> dict | None:
    return db.fetch_one("select * from story_bibles where series_id = %s order by version desc limit 1", (series_id,))


def speaking_codes(series_id) -> set[str]:
    bible = latest_bible(series_id)
    if not bible:
        return set()
    return {c["code"] for c in bible["content"].get("characters", []) if c.get("speaks", True)}


def batch_episodes(series_id, batch: str, statuses: tuple[str, ...] | None = None) -> list[dict]:
    sql = "select * from episodes where series_id = %s and batch = %s"
    params: list = [series_id, batch]
    if statuses:
        sql += " and status = any(%s)"
        params.append(list(statuses))
    return db.fetch_all(sql + " order by number", params)


def set_jsonb(conn, sql: str, *params):
    return conn.execute(sql, tuple(Jsonb(p) if isinstance(p, (dict, list)) else p for p in params))


def batch_shots(series_id, batch: str, statuses) -> list[dict]:
    return db.fetch_all(
        "select s.*, e.number episode_number, cardinality(s.line_ids) line_count from shots s join episodes e on e.id = s.episode_id"
        " where s.series_id = %s and e.batch = %s and s.status = any(%s) order by e.number, s.idx", (series_id, batch, list(statuses)),
    )
