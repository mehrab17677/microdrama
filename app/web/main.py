"""Web process: pages, API, fal webhook, event stream."""
from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.web.deps import get_current_user
from app.web.routes import auth as auth_routes
from app.web.routes import library as library_routes
from app.web.routes import media as media_routes
from app.web.routes import pages as pages_routes
from app.web.routes import productions as production_routes
from app.web.routes import team as team_routes
from app.web.routes import webhooks as webhook_routes
from app.web.templating import templates


def _secret() -> str:
    s = get_settings()
    if s.session_secret:
        return s.session_secret
    if s.dry_run:
        return "dev-only-secret"  # never used outside DRY_RUN
    raise RuntimeError("SESSION_SECRET is not set")


app = FastAPI(title="microdrama")
app.add_middleware(SessionMiddleware, secret_key=_secret(), https_only=True, same_site="lax", session_cookie="md_session")
app.include_router(auth_routes.router)
app.include_router(team_routes.router)
app.include_router(library_routes.router)
app.include_router(media_routes.router)
app.include_router(production_routes.router)
app.include_router(pages_routes.router)
app.include_router(webhook_routes.router)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    wants_html = "text/html" in request.headers.get("accept", "")
    if exc.status_code == 401 and wants_html and request.method == "GET":
        return RedirectResponse("/auth/login", status_code=303)
    if wants_html:
        return templates.TemplateResponse(request, "error.html", {"status": exc.status_code, "detail": exc.detail, "user": None}, status_code=exc.status_code)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}
