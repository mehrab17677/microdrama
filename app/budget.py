"""Video budget enforcement (guide 7.5).

A budget row exists per series and batch. A video-class job in phase 'submit' is charged its
estimate when a worker claims it; exceeding 125% (plus approved extra) holds the series.
The batch of a job comes from its episode (episodes.batch), else from series.batch.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from app import alerts, audit
from app.models.enums import AlertKind, SeriesStatus

log = logging.getLogger("budget")
HOLD_FACTOR = Decimal("1.25")


def batch_for_job(conn, job: dict) -> str | None:
    if job.get("episode_id"):
        row = conn.execute("select batch from episodes where id = %s", (job["episode_id"],)).fetchone()
        if row:
            return row["batch"]
    if job.get("series_id"):
        row = conn.execute("select batch from series where id = %s", (job["series_id"],)).fetchone()
        return row["batch"] if row else None
    return None


def create_budget(conn, series_id, batch: str, budget_usd) -> None:
    """Called once when clips are first enqueued for a batch (sum of one attempt of every planned
    video job). Re-running it leaves the existing row alone."""
    conn.execute(
        "insert into budgets (series_id, batch, budget_usd) values (%s, %s, %s) on conflict (series_id, batch) do nothing",
        (series_id, batch, Decimal(str(budget_usd))),
    )


def _recipients(conn, series_id, with_admin: bool) -> list:
    owner = conn.execute("select owner_id from series where id = %s", (series_id,)).fetchone()["owner_id"]
    ids = [owner] + (alerts.admin_ids(conn) if with_admin else [])
    return ids


def charge_at_claim(conn, job: dict) -> bool:
    """Run inside the claim transaction. True means the job may be claimed (and has been charged)."""
    series_id = job["series_id"]
    batch = batch_for_job(conn, job)
    budget = None
    if batch:
        budget = conn.execute("select * from budgets where series_id = %s and batch = %s for update", (series_id, batch)).fetchone()
    if budget is None:
        log.error("video job %s has no budget row (series %s, batch %s); leaving it queued", job["id"], series_id, batch)
        return False

    estimate = Decimal(job["estimated_cost"])
    limit = budget["budget_usd"] * HOLD_FACTOR + budget["approved_extra_usd"]
    if budget["spent_usd"] + estimate > limit:
        prev = conn.execute("select status from series where id = %s for update", (series_id,)).fetchone()["status"]
        if prev != SeriesStatus.held_budget:
            conn.execute("update series set status = 'held_budget', updated_at = now() where id = %s", (series_id,))
            audit.log(conn, None, "budget.hold", series_id, {"previous_status": prev, "batch": batch})
            alerts.notify(
                _recipients(conn, series_id, True), AlertKind.budget_125,
                f"Video spend reached 125% of the {batch} budget. Approve more to continue.",
                f"/productions/{series_id}", series_id, conn=conn,
            )
        return False

    spent = budget["spent_usd"] + estimate
    alert_100 = spent > budget["budget_usd"] and not budget["alerted_100"]
    conn.execute(
        "update budgets set spent_usd = %s, alerted_100 = alerted_100 or %s where id = %s",
        (spent, alert_100, budget["id"]),
    )
    if alert_100:
        alerts.notify(
            _recipients(conn, series_id, False), AlertKind.budget_100,
            f"Video spend passed 100% of the {batch} budget.", f"/productions/{series_id}", series_id, conn=conn,
        )
    return True


def adjust_spent(conn, job: dict, delta) -> None:
    """After completion: actual - estimated. After failure: minus the estimate."""
    batch = batch_for_job(conn, job)
    if not batch or not job.get("series_id"):
        return
    conn.execute(
        "update budgets set spent_usd = spent_usd + %s where series_id = %s and batch = %s",
        (Decimal(str(delta)), job["series_id"], batch),
    )


def approve_budget(conn, series_id, batch: str, amount_usd, user_id) -> None:
    """Add a stated amount and put the series back to the status it had before the hold."""
    amount = Decimal(str(amount_usd))
    if amount <= 0:
        raise ValueError("amount must be positive")
    conn.execute(
        "update budgets set approved_extra_usd = approved_extra_usd + %s where series_id = %s and batch = %s",
        (amount, series_id, batch),
    )
    status = conn.execute("select status from series where id = %s for update", (series_id,)).fetchone()["status"]
    if status == SeriesStatus.held_budget:
        hold = conn.execute(
            "select detail from audit_log where series_id = %s and action = 'budget.hold' order by created_at desc limit 1",
            (series_id,),
        ).fetchone()
        previous = (hold["detail"].get("previous_status") if hold else None) or SeriesStatus.waiting_jobs
        conn.execute("update series set status = %s, updated_at = now() where id = %s", (previous, series_id))
    audit.log(conn, user_id, "budget.approve", series_id, {"batch": batch, "amount_usd": str(amount)})
