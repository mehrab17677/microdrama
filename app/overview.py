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


# Which job kinds belong to which working stage, so a stage can show "done of total jobs". Gates have no jobs.
STAGE_KINDS = {
    "story": ["ingest_source", "llm_story_check", "llm_bible", "llm_beats", "llm_placement_plan", "llm_script", "llm_continuity", "llm_wardrobe", "llm_originality"],
    "visual": ["llm_trait_sheet", "llm_product_sheet", "llm_voice_profile", "voice_design", "image_master", "image_pack", "image_set_plate", "sfx_ambience"],
    "build": ["llm_shot_list", "llm_audio_tag", "tts_line", "image_keyframe", "sfx_effect", "music_episode", "llm_shot_request", "video_clip", "lipsync", "ffmpeg_insert", "ffmpeg_compile"],
    "qa": ["qa_shot", "qa_episode", "qa_brand", "llm_rule"],
    "delivery": ["ffmpeg_master", "llm_marketing"],
}
KIND_STAGE = {kind: stage for stage, kinds in STAGE_KINDS.items() for kind in kinds}
WAITING_WEIGHT = 0.5      # a gate that is open counts half: the work before it is finished, the decision is not


def stage_jobs(series_id) -> dict[str, dict]:
    """Per working stage: jobs done, failed, active (queued, running or waiting on a vendor) and total, from the job ledger."""
    out = {stage: {"done": 0, "failed": 0, "active": 0, "total": 0} for stage in STAGE_KINDS}
    for r in db.fetch_all("select kind, status, count(*) n from jobs where series_id = %s and status <> 'cancelled' group by kind, status", (series_id,)):
        stage = KIND_STAGE.get(r["kind"])
        if stage is None:
            continue
        bucket = out[stage]
        bucket["total"] += r["n"]
        bucket["done" if r["status"] == "succeeded" else "failed" if r["status"] == "failed" else "active"] += r["n"]
    return out


def progress(s: dict, jobs: dict[str, dict] | None = None) -> dict:
    """What the live production view draws: the overall percentage and, for every stage, its state and job counts.
    A running stage is drawn as an indeterminate ring (the work behind it is uneven); the counts say how far it has got."""
    jobs = jobs if jobs is not None else stage_jobs(s["id"])
    stages = []
    for st in tracker(s):
        counts = jobs.get(st["key"], {"done": 0, "failed": 0, "active": 0, "total": 0})
        stages.append({**st, **counts})
    cur = STAGES.index(s["current_stage"])
    if s["status"] == "done":
        percent = 100.0
    else:
        now = stages[cur]
        if now["state"] == "waiting":
            fraction = WAITING_WEIGHT
        elif now["total"]:
            fraction = min(0.95, now["done"] / now["total"])
        else:
            fraction = 0.05 if now["state"] == "running" else 0.0
        percent = min(99.0, (cur + fraction) / len(STAGES) * 100)
    failure = None
    if s["status"] == "failed":
        from app import retry

        row = db.fetch_one("select kind, error from jobs where series_id = %s and status = 'failed' order by coalesce(finished_at, created_at) desc limit 1", (s["id"],))
        info = retry.failure_info(None, s["id"]) or {"step": "", "message": ""}
        failure = {"kind": row["kind"] if row else "", "error": row["error"] if row else "", "step": info["step"], "step_label": retry.step_label(info["step"]) if info["step"] else "",
                   "message": info["message"], "jobs": retry.failed_jobs(s["id"])}
    return {"percent": round(percent, 1), "stages": stages, "failure": failure, "running": s["status"] in ("running", "waiting_jobs")}


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


def draft_pilot(series_id) -> dict | None:
    """When the pilot was rendered as lower-quality drafts: how many clips, what they cost at draft prices, and what the same clips and their lip-sync cost at full quality
    (the price of rendering the pilot again after the approval). None when nothing is a draft."""
    from app.media import costs

    rows = db.fetch_all(
        "select s.tier, s.fallback_used, s.spec, s.planned_duration, s.render_duration, cardinality(s.line_ids) line_count from shots s join episodes e on e.id = s.episode_id"
        " where s.series_id = %s and e.batch = 'pilot' and s.spec ->> 'rendered_quality' = 'draft'", (series_id,))
    if not rows:
        return None
    draft = final = 0.0
    for r in rows:
        args = (r["tier"], float(r["planned_duration"] or 0), r["render_duration"] or 0, r["line_count"], r["fallback_used"])
        draft += costs.shot_clip_cost(*args, False, "draft")
        final += costs.shot_clip_cost(*args, False, "final") + costs.shot_lipsync_cost(r["tier"], r["line_count"], r["render_duration"] or 0, r["fallback_used"])
    return {"clips": len(rows), "draft_cost": round(draft, 2), "upgrade_cost": round(final, 2)}


def projection(series_id) -> dict | None:
    """Gate 4: (video spend on the pilot / pilot seconds) x planned seconds of the remaining episodes. When the pilot was a lower-quality draft the pilot's spend is first
    raised to what it would have cost at full quality, so the projection is the real price of the full build; `draft` carries the cost of rendering the pilot again."""
    pilot = db.fetch_one("select spent_usd from budgets where series_id = %s and batch = 'pilot'", (series_id,))
    if pilot is None:
        return None
    pilot_seconds = _f(db.fetch_one("select coalesce(sum(runtime_seconds), 0) t from episodes where series_id = %s and batch = 'pilot'", (series_id,))["t"])
    if pilot_seconds <= 0:
        return None
    remaining = db.fetch_all("select runtime_seconds from episodes where series_id = %s and batch = 'full'", (series_id,))
    seconds = sum(_f(r["runtime_seconds"]) or AVERAGE_EPISODE_SECONDS for r in remaining)
    draft = draft_pilot(series_id)
    spend = _f(pilot["spent_usd"])
    at_full_quality = spend - draft["draft_cost"] + draft["upgrade_cost"] if draft else spend
    per_second = at_full_quality / pilot_seconds
    return {"pilot_spend": spend, "pilot_seconds": pilot_seconds, "per_second": per_second, "remaining_episodes": len(remaining), "remaining_seconds": seconds,
            "projected": per_second * seconds, "draft": draft}


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
    rows = [dict(r) for r in db.fetch_all(
        "select s.id, s.title, s.category, s.language, s.current_stage, s.status, u.display_name owner_name, s.updated_at,"
        " coalesce((select sum(cost_usd) from cost_log c where c.series_id = s.id), 0) spent from series s join users u on u.id = s.owner_id order by s.updated_at desc")]
    for r in rows:
        r["progress"] = progress(r)
    return rows


def recent_productions(limit: int = 8) -> list[dict]:
    """For the side panel: the latest productions with a status colour."""
    return [dict(r) for r in db.fetch_all("select id, title, status, current_stage from series order by updated_at desc limit %s", (limit,))]


def unread_alerts(user_id) -> int:
    return db.fetch_one("select count(*) n from alerts where user_id = %s and read_at is null", (user_id,))["n"]
