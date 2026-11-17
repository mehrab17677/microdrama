"""Poller process (guide 7.4): submitted jobs, stale workers, gate reminders. Runs every 20 s.

Webhooks are an optimisation: with no public URL everything still completes through here.
"""
from __future__ import annotations

import logging
import time
from typing import Callable

from app import alerts, db
from app.config import get_config, get_settings
from app.jobs import ledger
from app.models.enums import AlertKind

log = logging.getLogger("poller")
INTERVAL_SECONDS = 20
STALE_RUNNING_MINUTES = 15

# status_fn(job) -> ("pending", None) | ("done", payload) | ("error", message)
StatusFn = Callable[[dict], tuple[str, object]]


def default_status(job: dict) -> tuple[str, object]:
    if get_settings().dry_run:
        return "done", {"dry_run": True}  # dry-run jobs complete once they are old enough
    from app.vendors import falmedia  # Phase 3

    return falmedia.poll_status(job)


def poll_submitted(status_fn: StatusFn = default_status) -> dict:
    """Fail jobs past fal_timeout_minutes; ask the vendor about jobs submitted over 60 s ago
    (1 s in dry-run). Moves finished ones to the finalize phase exactly as the webhook does."""
    timeout = int(get_config("fal_timeout_minutes"))
    min_age = 1 if get_settings().dry_run else 60
    counts = {"timed_out": 0, "completed": 0, "failed": 0, "pending": 0}
    for job in db.fetch_all(
        "select *, extract(epoch from now() - submitted_at) / 60 as age_min from jobs"
        " where status = 'submitted' and submitted_at < now() - make_interval(secs => %s)", (min_age,)
    ):
        if job["age_min"] > timeout:
            if ledger.vendor_failed(job["id"], f"timed out after {timeout} minutes"):
                counts["timed_out"] += 1
            continue
        try:
            state, payload = status_fn(job)
        except Exception:  # noqa: BLE001
            log.exception("status check failed for job %s", job["id"])
            continue
        if state == "done":
            counts["completed"] += ledger.vendor_completed(job["id"], payload or {})
        elif state == "error":
            counts["failed"] += ledger.vendor_failed(job["id"], str(payload))
        else:
            counts["pending"] += 1
    return counts


def send_gate_reminders() -> int:
    """One reminder alert (owner and admin) per period for a series waiting at a gate."""
    hours = int(get_config("gate_reminder_hours"))
    sent = 0
    with db.transaction() as conn:
        due = conn.execute(
            "select s.id, s.owner_id, s.title from series s where s.status = 'waiting_gate'"
            " and s.updated_at < now() - make_interval(hours => %s)"
            " and not exists (select 1 from alerts a where a.series_id = s.id and a.kind = 'gate_reminder'"
            "                 and a.created_at > now() - make_interval(hours => %s))",
            (hours, hours),
        ).fetchall()
        for s in due:
            recipients = [s["owner_id"], *alerts.admin_ids(conn)]
            sent += alerts.notify(recipients, AlertKind.gate_reminder, f"“{s['title']}” has been waiting for a decision.", f"/productions/{s['id']}", s["id"], conn=conn)
    return sent


def tick(status_fn: StatusFn = default_status) -> dict:
    result = poll_submitted(status_fn)
    result["requeued"] = ledger.requeue_stale_running(STALE_RUNNING_MINUTES)
    result["reminders"] = send_gate_reminders()
    return result


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    while True:
        try:
            tick()
        except Exception:  # noqa: BLE001
            log.exception("poller tick failed")
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
