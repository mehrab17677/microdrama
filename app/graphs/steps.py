"""Enqueue a batch of related jobs for one graph step (guide 7.6, 8.2, 8.5).

enqueue_step is idempotent: it selects nothing itself, it only turns the node's work items into
ledger rows. A failed row bumps the attempt number (up to max_attempts) and the new attempts go in a
new wait group, so earlier failures never count against the retry.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app import db
from app.config import get_config
from app.graphs import common
from app.jobs import ledger


class StepFailed(Exception):
    """Work items ran out of attempts and the step cannot continue."""


@dataclass
class WorkItem:
    kind: str
    vendor: str
    request: dict
    endpoint: str | None = None
    episode_id: object = None
    shot_id: object = None
    budget_class: str = "none"
    estimated_cost: float = 0
    attempt: int = field(default=1, init=False)


def group_name(series_id, step: str, batch: str, rnd: int, level: int) -> str:
    base = f"{series_id}:{step}:{batch}:{rnd}"
    return base if level <= 1 else f"{base}:a{level}"


def enqueue_step(series_id, step: str, batch: str, rnd: int, items: list[WorkItem]) -> tuple[str, list[WorkItem]]:
    """Returns ('waiting' | 'idle', exhausted_items). 'waiting' means the series is now
    waiting_jobs on a new group; 'idle' means nothing is in flight. Items that used up max_attempts
    are returned in `exhausted` and not enqueued."""
    max_attempts = int(get_config("max_attempts")) * (1 + common.retry_epoch(series_id, step))   # each retry after a final failure gives the step a fresh set of attempts
    exhausted: list[WorkItem] = []
    live: list[WorkItem] = []
    with db.transaction() as conn:
        for item in items:
            attempt = 1
            while True:
                key = ledger.idempotency_key(item.kind, series_id, item.episode_id, item.shot_id, attempt, item.request)
                row = conn.execute("select status from jobs where idempotency_key = %s", (key,)).fetchone()
                if row and row["status"] == "failed":
                    attempt += 1
                    if attempt > max_attempts:
                        break
                    continue
                break
            if attempt > max_attempts:
                exhausted.append(item)
                continue
            item.attempt = attempt
            live.append(item)
        level = max([i.attempt for i in live], default=1)
        group = group_name(series_id, step, batch, rnd, level)
        active_ids = []
        for item in live:
            row = ledger.enqueue(
                item.kind, series_id=series_id, episode_id=item.episode_id, shot_id=item.shot_id, wait_group=group, vendor=item.vendor,
                endpoint=item.endpoint, request=item.request, budget_class=item.budget_class, estimated_cost=item.estimated_cost,
                attempt=item.attempt, conn=conn,
            )
            if row["status"] in ledger.ACTIVE:
                active_ids.append(row["id"])
        if active_ids:
            # In-flight rows from a node re-run may carry an older group name; unify them.
            conn.execute("update jobs set wait_group = %s where id = any(%s)", (group, active_ids))
            conn.execute("update series set status = 'waiting_jobs', waiting_group = %s, updated_at = now() where id = %s", (group, series_id))
            return "waiting", exhausted
        conn.execute("update series set waiting_group = null, updated_at = now() where id = %s", (series_id,))
        return "idle", exhausted
