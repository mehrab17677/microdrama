"""In-process simulation of the four processes for graph tests: workers, poller, runner."""
from __future__ import annotations

from app import db
from app.graphs.runner import resume_finished_groups
from app.jobs import poller, worker


def run_jobs(limit: int = 100000) -> int:
    n = 0
    while n < limit and worker.run_one("sim"):
        n += 1
    return n


def snapshot(series_id) -> str:
    s = db.fetch_one("select status, current_stage, batch, waiting_group from series where id = %s", (series_id,))
    jobs = db.fetch_all("select status, count(*) n from jobs where series_id = %s group by status order by status", (series_id,))
    cmds = db.fetch_all("select kind, status from graph_commands where series_id = %s order by created_at", (series_id,))
    return f"series={dict(s)} jobs={[(j['status'], j['n']) for j in jobs]} commands={[(c['kind'], c['status']) for c in cmds][-6:]}"


def step(runner) -> int:
    """One turn of everything. Returns how much happened."""
    moved = run_jobs()
    db.execute("update jobs set submitted_at = now() - interval '5 seconds' where status = 'submitted'")  # fal 'finished' a while ago
    moved += poller.tick()["completed"]
    moved += resume_finished_groups()
    moved += runner.process_commands()
    return moved


def drive(runner, series_id, until, max_turns: int = 600):
    """Advance until `until(series_row)` is true. Fails with a snapshot if it stalls."""
    for _ in range(max_turns):
        s = db.fetch_one("select * from series where id = %s", (series_id,))
        if until(s):
            return s
        if step(runner) == 0:
            s = db.fetch_one("select * from series where id = %s", (series_id,))
            if until(s):
                return s
            raise AssertionError("stalled: " + snapshot(series_id))
    raise AssertionError("too many turns: " + snapshot(series_id))


def at_gate(n):
    return lambda s: s["status"] == "waiting_gate" and s["current_stage"] == f"gate{n}"


def is_status(*names):
    return lambda s: s["status"] in names
