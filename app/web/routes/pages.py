"""Dashboard, alerts, the event stream, feedback, budget and the Gate 4/5 review page (guide 12.3, 12.7, 12.8)."""
from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from app import audit, budget, db, decisions, events, feedback, overview, placement
from app.web.deps import get_current_user, require_decider
from app.web.templating import templates

router = APIRouter()


def _media(series_id, key):
    prefix = f"series/{series_id}/"
    return f"/media/{series_id}/{key[len(prefix):]}" if key and key.startswith(prefix) else None


# ---------------------------------------------------------------- dashboard and alerts

@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user: dict = Depends(get_current_user)):
    return templates.TemplateResponse(request, "home.html", {"user": user, "waiting": overview.waiting_for(user), "rows": overview.dashboard_rows(),
                                                             "watch": ",".join(str(r["id"]) for r in overview.dashboard_rows())})


@router.get("/alerts", response_class=HTMLResponse)
def alert_list(request: Request, user: dict = Depends(get_current_user)):
    rows = db.fetch_all("select * from alerts where user_id = %s order by created_at desc limit 100", (user["id"],))
    return templates.TemplateResponse(request, "alerts.html", {"user": user, "alerts": rows})


@router.post("/alerts/{alert_id}/read")
def alert_read(alert_id: uuid.UUID, user: dict = Depends(get_current_user)):
    row = db.fetch_one("update alerts set read_at = now() where id = %s and user_id = %s and read_at is null returning link", (alert_id, user["id"]))
    if row is None and not db.fetch_one("select 1 from alerts where id = %s and user_id = %s", (alert_id, user["id"])):
        raise HTTPException(404, "Alert not found.")
    return RedirectResponse("/alerts", status_code=303)


@router.get("/events")
async def event_stream(user: dict = Depends(get_current_user), series: str = Query("")):
    """The signed-in user's alerts and the progress of the productions the page shows (?series=id,id)."""
    ids = []
    for part in series.split(","):
        try:
            ids.append(uuid.UUID(part.strip()))
        except ValueError:
            continue
    return StreamingResponse(events.stream(user["id"], ids), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- money

@router.post("/productions/{series_id}/budget")
def budget_decision(ctx: dict = Depends(require_decider), decision: str = Form("approve"), amount: str = Form(""), batch: str = Form("")):
    sid, uid = ctx["series"]["id"], ctx["user"]["id"]
    if decision == "decline":
        with db.transaction() as conn:
            audit.log(conn, uid, "budget.decline", sid, {})
        return RedirectResponse(f"/productions/{sid}", status_code=303)
    if decision != "approve":
        raise HTTPException(400, "Choose approve or decline.")
    try:
        value = Decimal(amount)
        if value <= 0:
            raise InvalidOperation
    except InvalidOperation:
        raise HTTPException(400, "Enter an amount in dollars greater than zero.") from None
    with db.transaction() as conn:
        row = conn.execute("select batch from budgets where series_id = %s and (batch = %s or %s = '') order by batch = 'full' desc limit 1", (sid, batch, batch)).fetchone()
        if row is None:
            raise HTTPException(400, "This production has no video budget yet.")
        budget.approve_budget(conn, sid, row["batch"], value, uid)
    return RedirectResponse(f"/productions/{sid}", status_code=303)


# ---------------------------------------------------------------- Gate 4 and 5 review, feedback

@router.get("/productions/{series_id}/review/episodes", response_class=HTMLResponse)
def review_episodes(request: Request, series_id: uuid.UUID, user: dict = Depends(get_current_user)):
    s = db.fetch_one("select s.*, u.display_name owner_name from series s join users u on u.id = s.owner_id where s.id = %s", (series_id,))
    if s is None:
        raise HTTPException(404, "Production not found.")
    from app.config import get_config

    episodes = []
    for e in db.fetch_all("select * from episodes where series_id = %s and final_r2_key is not null order by number", (series_id,)):
        shots = db.fetch_all("select s.id, s.idx, s.tier, s.status, s.attempts, e2.start_seconds, e2.end_seconds from shots s left join edit_entries e2 on e2.shot_id = s.id and e2.cut_version = %s"
                             " where s.episode_id = %s order by s.idx", (e["cut_version"], e["id"]))
        qa = db.fetch_all("select check_name, passed, category, score from qa_results where episode_id = %s and shot_id is null and qa_round in ('qa1','qa2') order by created_at desc", (e["id"],))
        flagged = db.fetch_all("select sh.idx, q.check_name, q.category from qa_results q join shots sh on sh.id = q.shot_id where sh.episode_id = %s and q.passed = false order by sh.idx", (e["id"],))
        episodes.append({"e": e, "video": _media(series_id, e["final_r2_key"]), "captions": _media(series_id, e["subtitle_r2_key"]), "shots": shots,
                         "qa": list({q["check_name"]: q for q in reversed(qa)}.values()), "flagged": flagged})
    items = db.fetch_all("select f.*, u.display_name author from feedback_items f join users u on u.id = f.created_by where f.series_id = %s order by f.created_at", (series_id,))
    used = db.fetch_one("select count(*) n from gate_decisions where series_id = %s and gate = 4 and decision = 'feedback' and coalesce(notes, '') <> 'reassemble'", (series_id,))["n"]
    gate = int(s["current_stage"][4:]) if s["status"] == "waiting_gate" and s["current_stage"] in ("gate4", "gate5") else None
    can_decide = user["role"] == "admin" or s["owner_id"] == user["id"]
    return templates.TemplateResponse(request, "review_episodes.html", {
        "user": user, "s": s, "episodes": episodes, "items": items, "gate": gate, "can_decide": can_decide, "categories": feedback.CATEGORIES,
        "rework_left": gate == 4 and used < int(get_config("pilot_rework_rounds")), "rework_used": used, "spend": overview.spend(series_id),
        "projection": overview.projection(series_id) if gate == 4 else None, "confirmed": any(i["status"] == "confirmed" for i in items),
        "feedback_media": _media, "placements": _placement_rows(series_id),
    })


def _placement_rows(series_id) -> list[dict]:
    rows = placement.report(series_id)
    refs = {k: [{"label": a["label"], "url": _media(series_id, a["r2_key"])} for a in placement.reference_assets(series_id, k)] for k in set(r["kind"] for r in rows)}
    for r in rows:
        r["clip_url"] = _media(series_id, r["clip"])
        r["assets"] = refs.get(r["kind"], [])
        r["problems"] = (r["fidelity"] or {}).get("problems", [])
    return rows


@router.get("/productions/{series_id}/placements.csv")
def placement_report_csv(series_id: uuid.UUID, user: dict = Depends(get_current_user)):
    """The placement report for the brand: every placement with its episode and timestamp."""
    from fastapi.responses import Response

    if not db.fetch_one("select 1 from series where id = %s", (series_id,)):
        raise HTTPException(404, "Production not found.")
    return Response(placement.report_csv(series_id), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="placement-report-{series_id}.csv"'})


@router.post("/productions/{series_id}/feedback")
def feedback_create(ctx: dict = Depends(require_decider), entry: str = Form(""), reference: str = Form(""), note: str = Form("")):
    """Either `entry` (reference and note in one line) or `reference` and `note` separately."""
    try:
        if entry.strip() and not reference.strip():
            reference, note = feedback.split_entry(entry)
        with db.transaction() as conn:
            feedback.create_item(conn, ctx["series"]["id"], ctx["user"]["id"], reference, note)
    except feedback.FeedbackError as exc:
        raise HTTPException(exc.status, exc.detail) from None
    return RedirectResponse(f"/productions/{ctx['series']['id']}/review/episodes", status_code=303)


@router.post("/productions/{series_id}/feedback/send")
def feedback_send(ctx: dict = Depends(require_decider)):
    try:
        with db.transaction() as conn:
            decisions.decide_feedback(conn, ctx["series"]["id"], ctx["user"]["id"])
    except decisions.DecisionError as exc:
        raise HTTPException(exc.status, exc.detail) from None
    return RedirectResponse(f"/productions/{ctx['series']['id']}", status_code=303)


@router.post("/productions/{series_id}/feedback/{fid}/confirm")
def feedback_confirm(fid: uuid.UUID, ctx: dict = Depends(require_decider), category: str = Form("")):
    try:
        with db.transaction() as conn:
            feedback.confirm_item(conn, ctx["series"]["id"], fid, ctx["user"]["id"], category or None)
    except feedback.FeedbackError as exc:
        raise HTTPException(exc.status, exc.detail) from None
    return RedirectResponse(f"/productions/{ctx['series']['id']}/review/episodes", status_code=303)


@router.post("/productions/{series_id}/placements/{pid}/approve")
def placement_approve(pid: uuid.UUID, ctx: dict = Depends(require_decider), note: str = Form("")):
    try:
        with db.transaction() as conn:
            placement.approve(conn, ctx["series"]["id"], pid, ctx["user"]["id"], note)
    except placement.PlacementError as exc:
        raise HTTPException(exc.status, exc.detail) from None
    return RedirectResponse(f"/productions/{ctx['series']['id']}/review/episodes", status_code=303)


@router.post("/productions/{series_id}/placements/{pid}/reject")
def placement_reject(pid: uuid.UUID, ctx: dict = Depends(require_decider), note: str = Form("")):
    try:
        with db.transaction() as conn:
            placement.reject(conn, ctx["series"]["id"], pid, ctx["user"]["id"], note)
    except placement.PlacementError as exc:
        raise HTTPException(exc.status, exc.detail) from None
    return RedirectResponse(f"/productions/{ctx['series']['id']}/review/episodes", status_code=303)


@router.post("/productions/{series_id}/reassemble")
def reassemble(ctx: dict = Depends(require_decider), episodes: list[int] = Form(default=[])):
    if not episodes:
        raise HTTPException(400, "Choose the episodes to recompile.")
    try:
        with db.transaction() as conn:
            decisions.decide_feedback(conn, ctx["series"]["id"], ctx["user"]["id"], reassemble=episodes)
    except decisions.DecisionError as exc:
        raise HTTPException(exc.status, exc.detail) from None
    return RedirectResponse(f"/productions/{ctx['series']['id']}", status_code=303)
