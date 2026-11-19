"""Read-side helpers for the dashboard, production page and Gate 4 review (guide 12.8): spend, projection, stage tracker,
episode grid, activity feed and the "waiting for you" list. Pure reads of Postgres."""
from __future__ import annotations

from decimal import Decimal

from app import db

STAGES = ["gate0", "gate1", "story", "gate2", "visual", "gate3", "build", "qa", "gate4", "gate5", "delivery", "done"]
STAGE_LABELS = {"gate0": "Gate 0 category", "gate1": "Gate 1 intake", "story": "Story", "gate2": "Gate 2 story review", "visual": "Look", "gate3": "Gate 3 look review",
                "build": "Build", "qa": "QA", "gate4": "Gate 4 pilot", "gate5": "Gate 5 final cut", "delivery": "Delivery", "done": "Done"}
AVERAGE_EPISODE_SECONDS = 82.5   # the middle of the 75 to 90 s target, used until an episode has a measured runtime
BUDGET_MARKS = (1.0, 1.25)


def _f(x) -> float:
    return float(x) if x is not None else 0.0


def tracker(s: dict) -> list[dict]:
    """Every stage and gate in order, each done, running, waiting or not started."""
    cur = STAGES.index(s["current_stage"])
    out = []
    for i, st in enumerate(STAGES):
        if s["status"] == "done" or i < cur:
            state = "done"
        elif i == cur:
            state = "waiting" if s["status"] in ("waiting_gate", "held_budget") else "running" if s["status"] in ("running", "waiting_jobs") else "waiting"
            if s["status"] in ("failed", "discarded"):
                state = s["status"]
        else:
            state = "not started"
        out.append({"key": st, "label": STAGE_LABELS[st], "state": state})
    return out


def spend(series_id) -> dict:
    """Per batch: budget, extra approved, spent and the marks at 100% and 125%; plus the total cost from cost_log."""
    rows = db.fetch_all("select * from budgets where series_id = %s order by batch", (series_id,))
    batches = []
    for b in rows:
        budget, extra, spent = _f(b["budget_usd"]), _f(b["approved_extra_usd"]), _f(b["spent_usd"])
        limit = budget * 1.25 + extra
        batches.append({"batch": b["batch"], "budget": budget, "extra": extra, "spent": spent, "limit": limit, "percent": (spent / budget * 100) if budget else 0.0,
                        "bar_percent": min(100.0, spent / limit * 100) if limit else 0.0, "marks": [round(m * budget / limit * 100, 1) if limit else 0 for m in BUDGET_MARKS]})
    total = db.fetch_one("select coalesce(sum(cost_usd), 0) t from cost_log where series_id = %s", (series_id,))["t"]
    return {"batches": batches, "total_cost": _f(total), "video_spent": sum(b["spent"] for b in batches)}


def projection(series_id) -> dict | None:
    """Gate 4: (video spend on the pilot / pilot seconds) x planned seconds of the remaining episodes."""
    pilot = db.fetch_one("select spent_usd from budgets where series_id = %s and batch = 'pilot'", (series_id,))
    if pilot is None:
        return None
    pilot_seconds = _f(db.fetch_one("select coalesce(sum(runtime_seconds), 0) t from episodes where series_id = %s and batch = 'pilot'", (series_id,))["t"])
    if pilot_seconds <= 0:
        return None
    remaining = db.fetch_all("select runtime_seconds from episodes where series_id = %s and batch = 'full'", (series_id,))
    seconds = sum(_f(r["runtime_seconds"]) or AVERAGE_EPISODE_SECONDS for r in remaining)
    per_second = _f(pilot["spent_usd"]) / pilot_seconds
    return {"pilot_spend": _f(pilot["spent_usd"]), "pilot_seconds": pilot_seconds, "per_second": per_second, "remaining_episodes": len(remaining), "remaining_seconds": seconds,
            "projected": per_second * seconds}


def episode_grid(series_id) -> list[dict]:
    return [dict(r) for r in db.fetch_all(
        "select e.number, e.status, e.batch, e.cut_version, e.runtime_seconds, count(s.id) shots_total,"
        " count(s.id) filter (where s.status in ('ready','qa_passed')) shots_ready from episodes e left join shots s on s.episode_id = e.id"
        " where e.series_id = %s group by e.id order by e.number", (series_id,))]


def activity(series_id, limit: int = 30) -> list[dict]:
    """The audit log and recent job changes of one production, newest first. Each source has its own share so a burst of jobs does not push the decisions out."""
    half = max(1, limit // 2)
    rows = db.fetch_all(
        "(select created_at at, 'audit' source, action what, '' extra from audit_log where series_id = %s and action not like 'note:%%' order by created_at desc limit %s)"
        " union all (select coalesce(finished_at, created_at) at, 'job' source, kind || ' ' || status what, coalesce(left(error, 120), '') extra from jobs where series_id = %s"
        " and status in ('succeeded','failed','submitted') order by coalesce(finished_at, created_at) desc limit %s)"
        " order by at desc", (series_id, half, series_id, half))
    return [dict(r) for r in rows]


def waiting_for(user: dict) -> list[dict]:
    """Open gates and budget approvals this user can act on (the owner, or the admin for every production)."""
    where = "" if user["role"] == "admin" else "and s.owner_id = %s"
    params = [] if user["role"] == "admin" else [user["id"]]
    rows = db.fetch_all(
        f"select s.id, s.title, s.status, s.current_stage from series s where (s.status = 'waiting_gate' or s.status = 'held_budget') {where} order by s.updated_at desc", params)
    out = []
    for r in rows:
        if r["status"] == "held_budget":
            out.append({"id": r["id"], "title": r["title"], "what": "Budget approval needed", "link": f"/productions/{r['id']}"})
        else:
            gate = r["current_stage"]
            link = {"gate2": "review/story", "gate3": "review/look", "gate4": "review/episodes", "gate5": "review/episodes"}.get(gate, "")
            out.append({"id": r["id"], "title": r["title"], "what": STAGE_LABELS.get(gate, gate), "link": f"/productions/{r['id']}" + (f"/{link}" if link else "")})
    return out


def dashboard_rows() -> list[dict]:
    return [dict(r) for r in db.fetch_all(
        "select s.id, s.title, s.category, s.language, s.current_stage, s.status, u.display_name owner_name, s.updated_at,"
        " coalesce((select sum(cost_usd) from cost_log c where c.series_id = s.id), 0) spent from series s join users u on u.id = s.owner_id order by s.updated_at desc")]


def unread_alerts(user_id) -> int:
    return db.fetch_one("select count(*) n from alerts where user_id = %s and read_at is null", (user_id,))["n"]
