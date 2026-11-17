"""Productions: start one (guide 12.4), view it, decide gates (12.5). The web process only reads and
writes Postgres; it never runs the graph."""
from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app import db, decisions, intake, library
from app.web.deps import get_current_user, require_decider
from app.web.templating import templates

router = APIRouter()

STAGES = ["gate0", "gate1", "story", "gate2", "visual", "gate3", "build", "qa", "gate4", "gate5", "delivery", "done"]


def _read(files: list[UploadFile]) -> list[tuple[str, bytes]]:
    return [(f.filename, f.file.read()) for f in files if f.filename]


@router.get("/productions/new", response_class=HTMLResponse)
def new_production(request: Request, category: str = "", user: dict = Depends(get_current_user)):
    return templates.TemplateResponse(request, "production_new.html", {
        "user": user, "category": category if category in ("standard", "ad_placement") else "", "genres": library.ready_genres(),
        "errors": [], "alert_text": intake.NOT_LISTED_ALERT, "placement_types": intake.PLACEMENT_TYPES, "asset_kinds": intake.BRAND_ASSET_KINDS,
    })


@router.post("/productions")
def create_production(
    request: Request,
    category: str = Form(""),
    genre_id: str = Form(""),
    custom_genre: str = Form(""),
    story_text: str = Form(""),
    keywords: str = Form(""),
    language: str = Form(""),
    aesthetic_note: str = Form(""),
    char_roles: list[str] = Form(default=[]),
    char_files: list[UploadFile] = File(default=[]),
    aesthetic_files: list[UploadFile] = File(default=[]),
    brand_name: str = Form(""),
    product: str = Form(""),
    placement_types: list[str] = Form(default=[]),
    frequency_note: str = Form(""),
    exclusions: str = Form(""),
    spoken_wording: str = Form(""),
    brand_kinds: list[str] = Form(default=[]),
    brand_labels: list[str] = Form(default=[]),
    brand_files: list[UploadFile] = File(default=[]),
    user: dict = Depends(get_current_user),
):
    chars = [f for f in char_files if f.filename]
    form = intake.IntakeForm(
        category=category, genre_id=genre_id or None, custom_genre=custom_genre, story_text=story_text, keywords=intake.parse_keywords(keywords),
        language=language, aesthetic_note=aesthetic_note,
        char_refs=[(char_roles[i] if i < len(char_roles) else "", f.file.read()) for i, f in enumerate(chars)],
        aesthetic_refs=[data for _, data in _read(aesthetic_files)],
        brand_name=brand_name, product=product, placement_types=placement_types, frequency_note=frequency_note,
        exclusions=intake.parse_keywords(exclusions), spoken_wording=spoken_wording,
        brand_assets=[((brand_kinds[i] if i < len(brand_kinds) else "product"), (brand_labels[i] if i < len(brand_labels) else ""), name, data)
                      for i, (name, data) in enumerate(_read(brand_files))],
    )
    problems = intake.validate(form)
    if problems:
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"errors": problems}, status_code=400)
        return templates.TemplateResponse(request, "production_new.html", {
            "user": user, "category": category if category in ("standard", "ad_placement") else "", "genres": library.ready_genres(), "errors": problems,
            "alert_text": intake.NOT_LISTED_ALERT, "placement_types": intake.PLACEMENT_TYPES, "asset_kinds": intake.BRAND_ASSET_KINDS,
        }, status_code=400)
    series_id = intake.create_production(user["id"], form)
    return RedirectResponse(f"/productions/{series_id}", status_code=303)


@router.get("/productions/{series_id}", response_class=HTMLResponse)
def production_page(request: Request, series_id: uuid.UUID, user: dict = Depends(get_current_user)):
    s = db.fetch_one("select s.*, u.display_name owner_name from series s join users u on u.id = s.owner_id where s.id = %s", (series_id,))
    if s is None:
        raise HTTPException(404, "Production not found.")
    can_decide = user["role"] == "admin" or s["owner_id"] == user["id"]
    stage_index = STAGES.index(s["current_stage"])
    return templates.TemplateResponse(request, "production.html", {
        "user": user, "s": s, "can_decide": can_decide, "stages": STAGES, "stage_index": stage_index,
        "gate": int(s["current_stage"][4:]) if s["status"] == "waiting_gate" and s["current_stage"].startswith("gate") else None,
    })


@router.get("/productions/{series_id}/review/story", response_class=HTMLResponse)
def review_story(request: Request, series_id: uuid.UUID, user: dict = Depends(get_current_user)):
    """Gate 2 review: logline, cast, beat sheet, scripts, wardrobe plan, originality report, placement plan."""
    s = db.fetch_one("select s.*, u.display_name owner_name from series s join users u on u.id = s.owner_id where s.id = %s", (series_id,))
    if s is None:
        raise HTTPException(404, "Production not found.")
    bible = db.fetch_one("select content, version from story_bibles where series_id = %s order by version desc limit 1", (series_id,))
    episodes = db.fetch_all("select number, batch, beat, script from episodes where series_id = %s order by number", (series_id,))
    wardrobe = db.fetch_all(
        "select c.code character_code, c.name, w.code, w.description, w.scenes from wardrobe_items w join characters c on c.id = w.character_id"
        " where w.series_id = %s order by c.code, w.code", (series_id,))
    originality = db.fetch_one("select passed, detail from qa_results where series_id = %s and check_name = 'originality' order by created_at desc limit 1", (series_id,))
    placements = db.fetch_all("select episode_number, kind, description from placements where series_id = %s order by episode_number", (series_id,))
    can_decide = user["role"] == "admin" or s["owner_id"] == user["id"]
    return templates.TemplateResponse(request, "review_story.html", {
        "user": user, "s": s, "bible": bible and bible["content"], "episodes": episodes, "wardrobe": wardrobe, "originality": originality,
        "placements": placements, "at_gate": s["status"] == "waiting_gate" and s["current_stage"] == "gate2", "can_decide": can_decide,
    })


def _media(series_id, key: str | None) -> str | None:
    prefix = f"series/{series_id}/"
    return f"/media/{series_id}/{key[len(prefix):]}" if key and key.startswith(prefix) else None


@router.get("/productions/{series_id}/review/look", response_class=HTMLResponse)
def review_look(request: Request, series_id: uuid.UUID, user: dict = Depends(get_current_user)):
    """Gate 3 review: each character's pack beside the reference photo, set plates, and a voice sample per speaking character."""
    from app.stages import look as look_stage

    s = db.fetch_one("select s.*, u.display_name owner_name from series s join users u on u.id = s.owner_id where s.id = %s", (series_id,))
    if s is None:
        raise HTTPException(404, "Production not found.")
    refs = look_stage.character_refs(series_id)
    scores = {r["detail"]["character_id"]: r for r in db.fetch_all("select score, passed, detail from qa_results where series_id = %s and check_name = 'likeness'", (series_id,))}
    characters, voices = [], {}
    for c in db.fetch_all("select * from characters where series_id = %s order by code", (series_id,)):
        assets = db.fetch_all("select id, kind, tags, r2_key_review, r2_key_clean, version from character_assets where character_id = %s order by kind, created_at", (c["id"],))
        master = next((a for a in assets if a["kind"] == "master"), None)
        voice = next((a for a in assets if a["kind"] == "voice_sample"), None)
        if voice:
            voices[c["code"]] = voice["tags"].get("voice_id")
        ref = refs.get(c["id"])
        score = scores.get(str(c["id"]))
        characters.append({
            "c": c, "ref_url": ref and _media(series_id, ref["r2_key"]), "ref_role": ref and ref["role_label"], "master": master and {**master, "url": _media(series_id, master["r2_key_review"])},
            "packs": [{**a, "url": _media(series_id, a["r2_key_review"])} for a in assets if a["kind"] not in ("master", "voice_sample")],
            "voice": voice and {**voice, "url": _media(series_id, voice["r2_key_clean"])},
            "score": score and float(score["score"]), "too_similar": score and not score["passed"],
        })
    lineup = db.fetch_one("select id, r2_key_review from character_assets where series_id = %s and kind = 'lineup' limit 1", (series_id,))
    sets = [{**r, "plate_url": _media(series_id, r["plate_r2_key"]), "ambience_url": _media(series_id, r["ambience_r2_key"])} for r in db.fetch_all("select * from sets where series_id = %s order by code, time_of_day", (series_id,))]
    can_decide = user["role"] == "admin" or s["owner_id"] == user["id"]
    return templates.TemplateResponse(request, "review_look.html", {
        "user": user, "s": s, "characters": characters, "lineup": lineup and {**lineup, "url": _media(series_id, lineup["r2_key_review"])}, "sets": sets,
        "voices_json": json.dumps(voices), "at_gate": s["status"] == "waiting_gate" and s["current_stage"] == "gate3", "can_decide": can_decide,
    })


def _fail(exc: decisions.DecisionError):
    raise HTTPException(exc.status, exc.detail)


@router.post("/productions/{series_id}/gate/{n}")
def decide_gate(
    n: int,
    ctx: dict = Depends(require_decider),
    decision: str = Form("approve"),
    notes: str = Form(""),
    target: str = Form(""),
    voices: str = Form(""),
    story_text: str = Form(""),
    redo: list[str] = Form(default=[]),
):
    target = target or ",".join(redo)          # the review page sends one checkbox per asset to redo
    try:
        voice_map = json.loads(voices) if voices.strip() else {}
        if not isinstance(voice_map, dict):
            raise ValueError
    except ValueError:
        raise HTTPException(400, "voices must be a JSON object of character code to voice id.") from None
    try:
        with db.transaction() as conn:
            decisions.decide_gate(conn, ctx["series"]["id"], ctx["user"]["id"], n, decision, notes, target, voice_map, story_text)
    except decisions.DecisionError as exc:
        _fail(exc)
    return RedirectResponse(f"/productions/{ctx['series']['id']}", status_code=303)


@router.post("/productions/{series_id}/pilot")
def decide_pilot(ctx: dict = Depends(require_decider), decision: str = Form(""), notes: str = Form(""), category: str = Form("")):
    try:
        with db.transaction() as conn:
            decisions.decide_pilot(conn, ctx["series"]["id"], ctx["user"]["id"], decision, notes, category)
    except decisions.DecisionError as exc:
        _fail(exc)
    return RedirectResponse(f"/productions/{ctx['series']['id']}", status_code=303)
