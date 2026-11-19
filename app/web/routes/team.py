"""Team page: approve and disable users (admin only; guide 12.1)."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import audit, db
from app.models.enums import UserStatus
from app.web.auth import SEAT_CAP
from app.web.deps import require_admin
from app.web.templating import templates

router = APIRouter()


@router.get("/team", response_class=HTMLResponse)
def team_page(request: Request, admin: dict = Depends(require_admin)):
    users = db.fetch_all("select id, email, display_name, role, status, created_at, approved_at from users order by created_at")
    return templates.TemplateResponse(request, "team.html", {
        "user": admin,
        "pending": [u for u in users if u["status"] == UserStatus.pending],
        "active": [u for u in users if u["status"] == UserStatus.active],
        "disabled": [u for u in users if u["status"] == UserStatus.disabled],
        "seats_used": sum(1 for u in users if u["status"] == UserStatus.active),
        "seat_cap": SEAT_CAP,
    })


@router.post("/team/{user_id}/approve")
def approve(user_id: uuid.UUID, admin: dict = Depends(require_admin)):
    with db.transaction() as conn:
        # One transaction: lock the table, count active rows, refuse at the cap.
        conn.execute("lock table users in exclusive mode")
        target = conn.execute("select id, status from users where id = %s", (user_id,)).fetchone()
        if target is None:
            raise HTTPException(404, "User not found.")
        if target["status"] != UserStatus.pending:
            raise HTTPException(409, "Only a pending request can be approved.")
        active = conn.execute("select count(*) n from users where status = 'active'").fetchone()["n"]
        if active >= SEAT_CAP:
            raise HTTPException(409, f"All {SEAT_CAP} seats are in use.")
        conn.execute("update users set status = 'active', approved_at = now() where id = %s", (user_id,))
        audit.log(conn, admin["id"], "user.approve", detail={"user_id": str(user_id)})
    return RedirectResponse("/team", status_code=303)


@router.post("/team/{user_id}/disable")
def disable(user_id: uuid.UUID, admin: dict = Depends(require_admin)):
    if user_id == admin["id"]:
        raise HTTPException(400, "The admin cannot disable themselves.")
    with db.transaction() as conn:
        updated = conn.execute("update users set status = 'disabled' where id = %s and role <> 'admin'", (user_id,)).rowcount
        if not updated:
            raise HTTPException(404, "User not found.")
        audit.log(conn, admin["id"], "user.disable", detail={"user_id": str(user_id)})
    return RedirectResponse("/team", status_code=303)
