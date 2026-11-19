"""GET /media/{series_id}/{path}: permission check, then the file (guide 12.3).
R2 objects are served by redirecting to a short-lived presigned URL; the local test store is served directly."""
from __future__ import annotations

import mimetypes
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, RedirectResponse

from app import db
from app.vendors import storage
from app.web.deps import get_current_user

router = APIRouter()
PRESIGN_SECONDS = 3600


@router.get("/media/{series_id}/{path:path}")
def media(series_id: uuid.UUID, path: str, user: dict = Depends(get_current_user)):
    parts = path.split("/")
    if not path or path.startswith("/") or ".." in parts or "" in parts:
        raise HTTPException(404, "Not found.")
    if db.fetch_one("select 1 from series where id = %s", (series_id,)) is None:
        raise HTTPException(404, "Not found.")
    key = f"series/{series_id}/{path}"
    if storage.is_local():
        file = storage.local_file(key)
        if not file.is_file():
            raise HTTPException(404, "Not found.")
        return FileResponse(file, media_type=mimetypes.guess_type(path)[0] or "application/octet-stream")
    return RedirectResponse(storage.presign_get(key, PRESIGN_SECONDS), status_code=307)
