"""The job ledger: enqueue (idempotent) and every status transition (guide section 7).

Handler results may carry two optional keys, removed before the result is stored:
  actual_cost: number (USD); defaults to the job's estimated_cost
  cost_log:    list of {vendor, model, units, unit_name, cost_usd} rows
"""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from decimal import Decimal

from psycopg.types.json import Jsonb

from app import budget, db

ACTIVE = ("queued", "running", "submitted")


@contextmanager
def _use(conn=None):
    if conn is not None:
        yield conn
    else:
        with db.transaction() as c:
            yield c


def canonical_json(request: dict) -> str:
    return json.dumps(request, sort_keys=True, separators=(",", ":"))


def idempotency_key(kind, series_id, episode_id, shot_id, attempt, request: dict) -> str:
    parts = [kind, *(("" if v is None else str(v)) for v in (series_id, episode_id, shot_id)), str(attempt), canonical_json(request)]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _reject_presigned(value) -> None:
    """Presigned URLs change every time, so they must never be part of request."""
    if isinstance(value, str) and ("X-Amz-Signature" in value or "X-Amz-Credential" in value):
        raise ValueError("a presigned URL must not be stored in a job request; store the R2 key instead")
    if isinstance(value, dict):
        for v in value.values():
            _reject_presigned(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _reject_presigned(v)


def touch_series(conn, series_id) -> None:
    if series_id:
        conn.execute("update series set updated_at = now() where id = %s", (series_id,))


def enqueue(kind, series_id=None, episode_id=None, shot_id=None, wait_group=None, vendor=None, endpoint=None,
            request=None, budget_class="none", estimated_cost=0, attempt=1, conn=None) -> dict:
    """Insert a job, or return the existing row for the same idempotency key unchanged.
    An existing row in 'succeeded' means the work is already done: use its result."""
    kind = str(kind)
    request = request or {}
    _reject_presigned(request)
    key = idempotency_key(kind, series_id, episode_id, shot_id, attempt, request)
    with _use(conn) as c:
        row = c.execute(
            "insert into jobs (series_id, episode_id, shot_id, kind, wait_group, idempotency_key, vendor, endpoint,"
            " request, status, attempt, budget_class, estimated_cost)"
            " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,'queued',%s,%s,%s)"
            " on conflict (idempotency_key) do nothing returning *",
            (series_id, episode_id, shot_id, kind, wait_group, key, vendor and str(vendor), endpoint,
             Jsonb(request), attempt, str(budget_class), Decimal(str(estimated_cost))),
        ).fetchone()
        if row is None:
            return c.execute("select * from jobs where idempotency_key = %s", (key,)).fetchone()
        touch_series(c, series_id)
        return row


def get_job(job_id, conn=None) -> dict | None:
    with _use(conn) as c:
        return c.execute("select * from jobs where id = %s", (job_id,)).fetchone()


def mark_submitted(job_id, vendor_request_id: str, conn=None) -> bool:
    with _use(conn) as c:
        row = c.execute(
            "update jobs set status = 'submitted', vendor_request_id = %s, submitted_at = now(), locked_by = null, locked_at = null"
            " where id = %s and status = 'running' returning series_id",
            (vendor_request_id, job_id),
        ).fetchone()
        if row:
            touch_series(c, row["series_id"])
        return row is not None


def succeed(job_id, result: dict | None, conn=None) -> bool:
    """Running -> succeeded. Writes cost_log rows and adjusts the budget. False if the job is no
    longer running (for example it was cancelled while the handler worked)."""
    result = dict(result or {})
    actual = result.pop("actual_cost", None)
    cost_rows = result.pop("cost_log", None) or []
    with _use(conn) as c:
        job = c.execute("select * from jobs where id = %s and status = 'running' for update", (job_id,)).fetchone()
        if job is None:
            return False
        actual = Decimal(job["estimated_cost"]) if actual is None else Decimal(str(actual))
        c.execute(
            "update jobs set status = 'succeeded', result = %s, actual_cost = %s, finished_at = now(), locked_by = null, locked_at = null where id = %s",
            (Jsonb(result), actual, job_id),
        )
        for r in cost_rows:
            c.execute(
                "insert into cost_log (series_id, job_id, vendor, model, units, unit_name, cost_usd) values (%s,%s,%s,%s,%s,%s,%s)",
                (job["series_id"], job_id, r["vendor"], r["model"], Decimal(str(r["units"])), r["unit_name"], Decimal(str(r["cost_usd"]))),
            )
        if job["budget_class"] == "video":
            budget.adjust_spent(c, job, actual - Decimal(job["estimated_cost"]))
        touch_series(c, job["series_id"])
        return True


def fail(job_id, error: str, conn=None) -> bool:
    """Running or submitted -> failed. The next attempt is created by the repair logic, not here."""
    with _use(conn) as c:
        job = c.execute("select * from jobs where id = %s and status in ('running','submitted') for update", (job_id,)).fetchone()
        if job is None:
            return False
        c.execute(
            "update jobs set status = 'failed', error = %s, finished_at = now(), locked_by = null, locked_at = null where id = %s",
            (error[:2000], job_id),
        )
        if job["budget_class"] == "video":
            budget.adjust_spent(c, job, -Decimal(job["estimated_cost"]))  # the estimate was charged at claim
        touch_series(c, job["series_id"])
        return True


def vendor_completed(job_id, payload: dict, conn=None) -> bool:
    """Webhook or poller reports success: submitted -> queued in phase 'finalize'. Idempotent:
    returns False (and changes nothing) if the job is no longer 'submitted'."""
    with _use(conn) as c:
        row = c.execute(
            "update jobs set status = 'queued', phase = 'finalize', result = %s where id = %s and status = 'submitted' returning series_id",
            (Jsonb(payload), job_id),
        ).fetchone()
        if row:
            touch_series(c, row["series_id"])
        return row is not None


def vendor_failed(job_id, error: str, conn=None) -> bool:
    return fail(job_id, error, conn)


def requeue_stale_running(minutes: int = 15, conn=None) -> int:
    """A job 'running' with locked_at older than `minutes` belongs to a dead worker: back to queued.
    A video job in phase 'submit' is refunded its estimate, because it is charged again on re-claim."""
    with _use(conn) as c:
        rows = c.execute(
            "update jobs set status = 'queued', locked_by = null, locked_at = null"
            " where status = 'running' and locked_at < now() - make_interval(mins => %s) returning *",
            (minutes,),
        ).fetchall()
        for job in rows:
            if job["budget_class"] == "video" and job["phase"] == "submit":
                budget.adjust_spent(c, job, -Decimal(job["estimated_cost"]))
            touch_series(c, job["series_id"])
        return len(rows)


def cancel_series_jobs(series_id, conn=None) -> int:
    """Gate 4 discard: cancel queued, running and submitted jobs. Nothing is refunded; a submitted
    clip is already with the vendor."""
    with _use(conn) as c:
        n = c.execute(
            "update jobs set status = 'cancelled', finished_at = now(), locked_by = null, locked_at = null"
            " where series_id = %s and status in ('queued','running','submitted')", (series_id,),
        ).rowcount
        touch_series(c, series_id)
        return n
