"""Library page, bulk upload, genre suggestions (guide 12.3). Any active member may use them."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from app import db, library
from app.web.deps import get_current_user
from app.web.templating import templates

router = APIRouter()


@router.get("/library", response_class=HTMLResponse)
def library_page(request: Request, batch: uuid.UUID | None = None, user: dict = Depends(get_current_user)):
    batches = library.recent_batches()
    genres = library.genre_suggestions("", limit=500)
    cards = db.fetch_all(
        "select s.id, s.file_name, s.source_card, g.name genre from sources s left join genres g on g.id = s.genre_id"
        " where s.status = 'ready' and s.series_id is null order by s.created_at desc limit 50"
    )
    return templates.TemplateResponse(request, "library.html", {"user": user, "batches": batches, "genres": genres, "cards": cards, "highlight": str(batch) if batch else None})


@router.post("/library/upload")
def library_upload(
    genre: str = Form(""),
    files: list[UploadFile] = File(default=[]),
    user: dict = Depends(get_current_user),
):
    uploads = [(f.filename or "", f.file.read()) for f in files if f.filename]
    try:
        batch_id = library.upload_batch(user["id"], genre, uploads)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return RedirectResponse(f"/library?batch={batch_id}", status_code=303)


@router.get("/library/genres")
def genre_suggestions(q: str = "", ready_only: bool = False, user: dict = Depends(get_current_user)):
    rows = library.genre_suggestions(q, ready_only=ready_only)
    return [{"id": str(r["id"]), "name": r["name"], "ready_sources": r["ready_sources"], "total_sources": r["total_sources"]} for r in rows]
