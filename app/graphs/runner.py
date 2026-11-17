"""Runner process: resumes graphs when job groups finish (guide 7.6).

Phase 2 builds the group-done loop. Executing the graph's start/resume commands arrives in Phase 5.
"""
from __future__ import annotations

import logging
import time

from psycopg.types.json import Jsonb

from app import db
from app.jobs.ledger import ACTIVE

log = logging.getLogger("runner")
INTERVAL_SECONDS = 2


def resume_finished_groups() -> int:
    """For each series waiting on a job group with no queued/running/submitted jobs, flip it to
    'running' and insert one resume command. The conditional UPDATE is the guard against two
    resumes for one group, even with several runner processes. Returns the number resumed."""
    resumed = 0
    waiting = db.fetch_all("select id, waiting_group from series where status = 'waiting_jobs' and waiting_group is not null")
    for s in waiting:
        with db.transaction() as conn:
            pending = conn.execute(
                "select 1 from jobs where wait_group = %s and status = any(%s) limit 1", (s["waiting_group"], list(ACTIVE))
            ).fetchone()
            if pending:
                continue
            row = conn.execute(
                "update series set status = 'running', updated_at = now()"
                " where id = %s and status = 'waiting_jobs' and waiting_group = %s returning id, thread_id",
                (s["id"], s["waiting_group"]),
            ).fetchone()
            if row is None:
                continue  # another runner got there first
            failed = conn.execute(
                "select count(*) n from jobs where wait_group = %s and status = 'failed'", (s["waiting_group"],)
            ).fetchone()["n"]
            conn.execute(
                "insert into graph_commands (series_id, graph, thread_id, kind, payload) values (%s, 'production', %s, 'resume', %s)",
                (row["id"], row["thread_id"], Jsonb({"type": "jobs_done", "group": s["waiting_group"], "failed": failed})),
            )
            resumed += 1
    return resumed


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    while True:
        try:
            resume_finished_groups()
        except Exception:  # noqa: BLE001
            log.exception("runner loop error")
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
