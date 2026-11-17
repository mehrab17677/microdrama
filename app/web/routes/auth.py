"""Register, sign in, sign out (guide 12.1)."""
from __future__ import annotations

import psycopg
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import alerts, audit, db
from app.models.enums import AlertKind, UserRole, UserStatus
from app.web import auth
from app.web.templating import templates

router = APIRouter()


@router.get("/auth/register", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse(request, "register.html", {"errors": [], "user": None})


@router.post("/auth/register", response_class=HTMLResponse)
def register(request: Request, email: str = Form(""), display_name: str = Form(""), password: str = Form("")):
    email = auth.normalize_email(email)
    errors = auth.validate_registration(email, display_name, password)
    if errors:
        return templates.TemplateResponse(request, "register.html", {"errors": errors, "user": None}, status_code=400)
    try:
        with db.transaction() as conn:
            row = conn.execute(
                "insert into users (email, display_name, password_hash, role, status) values (%s, %s, %s, %s, %s) returning id",
                (email, display_name.strip(), auth.hash_password(password), UserRole.member, UserStatus.pending),
            ).fetchone()
            alerts.notify(
                alerts.admin_ids(conn), AlertKind.access_request,
                f"{display_name.strip()} ({email}) asked for access.", "/team", conn=conn,
            )
            audit.log(conn, row["id"], "user.register")
    except psycopg.errors.UniqueViolation:
        return templates.TemplateResponse(request, "register.html", {"errors": ["That email is already registered."], "user": None}, status_code=409)
    return templates.TemplateResponse(request, "login.html", {"errors": [], "notice": "Request sent. You can sign in once the admin approves it.", "user": None}, status_code=201)


@router.get("/auth/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"errors": [], "notice": None, "user": None})


@router.post("/auth/login")
def login(request: Request, email: str = Form(""), password: str = Form("")):
    row = db.fetch_one("select id, password_hash, status from users where email = %s", (auth.normalize_email(email),))
    ok = auth.verify_password(password, row["password_hash"] if row else None)
    if not ok:
        return templates.TemplateResponse(request, "login.html", {"errors": ["Wrong email or password."], "notice": None, "user": None}, status_code=401)
    if row["status"] != UserStatus.active:
        msg = "Your access request is waiting for the admin." if row["status"] == UserStatus.pending else "This account has been disabled."
        return templates.TemplateResponse(request, "login.html", {"errors": [msg], "notice": None, "user": None}, status_code=403)
    request.session.clear()
    request.session["user_id"] = str(row["id"])  # only the user id goes in the cookie
    return RedirectResponse("/", status_code=303)


@router.post("/auth/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/auth/login", status_code=303)
