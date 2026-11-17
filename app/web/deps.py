"""Request dependencies: current user, admin, and require_decider (guide 12.2)."""
from __future__ import annotations

import uuid

from fastapi import Depends, HTTPException, Request

from app import db


def get_current_user(request: Request) -> dict:
    """Load the user on every request and reject unless status = 'active'."""
    raw = request.session.get("user_id")
    if not raw:
        raise HTTPException(401, "Sign in required.")
    try:
        uid = uuid.UUID(raw)
    except ValueError:
        request.session.clear()
        raise HTTPException(401, "Sign in required.")
    user = db.fetch_one("select id, email, display_name, role, status from users where id = %s", (uid,))
    if user is None or user["status"] != "active":
        request.session.clear()
        raise HTTPException(401, "This account is not active.")
    return user


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "Admin only.")
    return user


def require_decider(series_id: uuid.UUID, user: dict = Depends(get_current_user)) -> dict:
    """Admin or the owner of the production may decide. Every deciding route uses this.
    The route path must use the parameter name {series_id}. Returns {'user', 'series'}."""
    series = db.fetch_one("select id, owner_id, status, current_stage from series where id = %s", (series_id,))
    if series is None:
        raise HTTPException(404, "Production not found.")
    if user["role"] != "admin" and series["owner_id"] != user["id"]:
        raise HTTPException(403, "Only the owner or the admin can do this.")
    return {"user": user, "series": series}
