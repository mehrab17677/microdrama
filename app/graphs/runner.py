"""Runner process (guide 7.6 and 8.4): executes graph commands and resumes graphs when job groups finish.

The web process never imports or runs the graph; it only inserts graph_commands rows. Commands for
one series run strictly in created_at order, one at a time (a Postgres advisory lock per series).
"""
from __future__ import annotations

import logging
import time
import uuid

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app import db
from app.config import get_settings
from app.graphs import common
from app.graphs.production import build_production_graph
from app.jobs.ledger import ACTIVE

log = logging.getLogger("runner")
INTERVAL_SECONDS = 2
RECURSION_LIMIT = 1000  # a build loop passes through many nodes between pauses
LOCK_NAMESPACE = 7_364_003


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


class GraphRunner:
    """Holds the checkpointer connection and the compiled graph for this process."""

    def __init__(self, url: str | None = None):
        self._conn = psycopg.connect(url or get_settings().database_url, autocommit=True, row_factory=dict_row, prepare_threshold=None)
        self.checkpointer = PostgresSaver(self._conn)
        self.graph = build_production_graph().compile(checkpointer=self.checkpointer)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- commands
    def _invoke(self, command: dict) -> None:
        """Run one command against the series' thread. The checkpoint decides what is right, so a command
        re-run after a crash never repeats work: a pending pause gets the resume value; a run that was
        cut off mid-way carries on from its last checkpoint; a finished run is left alone."""
        config = {"configurable": {"thread_id": command["thread_id"]}, "recursion_limit": RECURSION_LIMIT}
        snap = self.graph.get_state(config)
        paused = any(getattr(t, "interrupts", ()) for t in snap.tasks)
        if command["kind"] == "start":
            if snap.created_at is None:
                series = common.get_series(command["series_id"])
                self.graph.invoke({"series_id": str(series["id"]), "batch": "pilot", "round": 0}, config)
            elif snap.next and not paused:
                self.graph.invoke(None, config)   # a start that was cut off
            else:
                log.warning("series %s already started; ignoring a second start", command["series_id"])
        elif paused:
            self.graph.invoke(Command(resume=command["payload"]), config)
        elif snap.next:
            self.graph.invoke(None, config)       # the resume was applied before a crash; carry on
        else:
            log.warning("resume for finished series %s ignored", command["series_id"])

    def process_commands(self, limit: int = 50) -> int:
        """Run queued commands, oldest first per series. Returns how many ran."""
        ran = 0
        self.recover_running()
        while ran < limit:
            row = db.fetch_one(
                "select c.* from graph_commands c where c.status = 'queued' and not exists"
                " (select 1 from graph_commands o where o.series_id = c.series_id and o.status in ('queued','running')"
                "  and (o.created_at, o.id) < (c.created_at, c.id)) order by c.created_at, c.id limit 1"
            )
            if row is None:
                break
            if not self._run_locked(row):
                break  # another runner holds this series; try again next tick
            ran += 1
        return ran

    def _run_locked(self, command: dict) -> bool:
        key = common_lock_key(command["series_id"])
        with db.connect(autocommit=True) as lock:
            if not lock.execute("select pg_try_advisory_lock(%s, %s) ok", key).fetchone()["ok"]:
                return False
            try:
                claimed = lock.execute("update graph_commands set status = 'running' where id = %s and status = 'queued' returning id", (command["id"],)).fetchone()
                if claimed is None:
                    return True
                try:
                    self._invoke(command)
                except Exception as exc:  # noqa: BLE001 - a bug or bad data: the series stops, with an alert
                    log.exception("graph command %s failed", command["id"])
                    lock.execute("update graph_commands set status = 'failed', error = %s where id = %s", (f"{type(exc).__name__}: {exc}"[:1000], command["id"]))
                    common.fail_series(command["series_id"], "graph", f"{type(exc).__name__}: {exc}"[:300])
                else:
                    lock.execute("update graph_commands set status = 'done' where id = %s", (command["id"],))
                return True
            finally:
                lock.execute("select pg_advisory_unlock(%s, %s)", key)

    def recover_running(self) -> int:
        """A command left 'running' by a dead runner (its advisory lock is gone) goes back to the queue.
        The graph re-runs from its last checkpoint; nodes are idempotent, so nothing is lost or repeated."""
        n = 0
        for row in db.fetch_all("select id, series_id from graph_commands where status = 'running'"):
            with db.connect(autocommit=True) as lock:
                key = common_lock_key(row["series_id"])
                if lock.execute("select pg_try_advisory_lock(%s, %s) ok", key).fetchone()["ok"]:
                    try:
                        n += lock.execute("update graph_commands set status = 'queued' where id = %s and status = 'running'", (row["id"],)).rowcount
                    finally:
                        lock.execute("select pg_advisory_unlock(%s, %s)", key)
        return n


def common_lock_key(series_id) -> tuple[int, int]:
    """Two-int advisory lock key: a namespace and a stable 31-bit hash of the series id."""
    return LOCK_NAMESPACE, uuid.UUID(str(series_id)).int % (2**31 - 1)


def main() -> None:
    from app import logging_setup

    logging_setup.setup()
    with GraphRunner() as runner:
        while True:
            try:
                resume_finished_groups()
                runner.process_commands()
            except Exception:  # noqa: BLE001
                log.exception("runner loop error")
            time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
