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
):
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
