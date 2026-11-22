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
MAX_FAILED_LOGINS = 5     # per email address in 15 minutes


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
    key = auth.normalize_email(email)
    # Failed attempts in the last 15 minutes, ignoring any from before the latest password reset for this address.
    recent = db.fetch_one(
        "select count(*) n from audit_log where action = 'auth.login_failed' and detail->>'email' = %s "
        "and created_at > greatest(now() - interval '15 minutes', coalesce((select max(created_at) from audit_log "
        "where action = 'auth.password_reset' and detail->>'email' = %s), '-infinity'))", (key, key))["n"]
    if recent >= MAX_FAILED_LOGINS:   # the same answer whether or not the account exists
        return templates.TemplateResponse(request, "login.html", {"errors": ["Too many failed attempts. Try again in 15 minutes."], "notice": None, "user": None}, status_code=429)
    row = db.fetch_one("select id, password_hash, status from users where email = %s", (key,))
    ok = auth.verify_password(password, row["password_hash"] if row else None)
    if not ok:
        with db.transaction() as conn:
            audit.log(conn, None, "auth.login_failed", None, {"email": key})
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
