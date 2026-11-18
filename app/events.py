"""Server-sent events (guide 12.7). Every POLL_SECONDS the stream looks for new alert rows for the user and for a change in
series.updated_at on the productions the page is showing, and sends an `alert` or `progress` event.
The web process never pushes from the workers: it reads Postgres, so any process can cause an event."""
from __future__ import annotations

import asyncio
import json

from app import db

POLL_SECONDS = 2.0


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def snapshot(series_ids: list) -> dict:
    if not series_ids:
        return {}
    return {str(r["id"]): r["updated_at"] for r in db.fetch_all("select id, updated_at from series where id = any(%s)", (series_ids,))}


def new_alerts(user_id, seen: set, since) -> list[dict]:
    """Alerts for the user not yet sent. `since` is a little before the connection time, so an alert whose transaction began a moment earlier is not missed."""
    return [r for r in db.fetch_all(
        "select id, series_id, kind, message, link, created_at from alerts where user_id = %s and created_at >= %s order by created_at", (user_id, since))
        if str(r["id"]) not in seen]


def changed(series_ids: list, known: dict) -> list[dict]:
    now = snapshot(series_ids)
    out = []
    for sid, ts in now.items():
        if sid not in known or ts != known[sid]:
            row = db.fetch_one("select id, status, current_stage from series where id = %s", (sid,))
            out.append({"series_id": sid, "status": row["status"], "stage": row["current_stage"]})
    known.clear()
    known.update(now)
    return out


async def stream(user_id, series_ids: list, poll: float = POLL_SECONDS, max_events: int | None = None):
    """Yields SSE text. `max_events` ends the stream after that many events (used by tests)."""
    since = (await asyncio.to_thread(db.fetch_one, "select now() - interval '5 seconds' t"))["t"]
    known = await asyncio.to_thread(snapshot, series_ids)
    seen: set = {str(r["id"]) for r in await asyncio.to_thread(new_alerts, user_id, set(), since)}   # alerts that already exist are not news
    sent = 0
    yield ": connected\n\n"
    while max_events is None or sent < max_events:
        await asyncio.sleep(poll)
        events = []
        for a in await asyncio.to_thread(new_alerts, user_id, seen, since):
            seen.add(str(a["id"]))
            events.append(_sse("alert", {"id": str(a["id"]), "series_id": a["series_id"], "kind": a["kind"], "message": a["message"], "link": a["link"]}))
        for c in await asyncio.to_thread(changed, series_ids, known):
            events.append(_sse("progress", c))
        if not events:
            yield ": keepalive\n\n"
            continue
        for e in events:
            sent += 1
            yield e
