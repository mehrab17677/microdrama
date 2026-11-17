"""POST /webhooks/fal: no session; the signature is the authentication (guide 6.4)."""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app import db
from app.jobs import ledger
from app.vendors import falwebhook

router = APIRouter()
log = logging.getLogger("webhook")


def handle_delivery(body: dict) -> tuple[int, dict]:
    """Apply one verified delivery. Only the state change happens here; the download and R2 copy
    run later in a worker (phase 'finalize')."""
    job = db.fetch_one("select id, status from jobs where vendor_request_id = %s", (body["request_id"],))
    if job is None:
        # Not 2xx on purpose: fal retries, and our row may not be saved yet. The poller is the backstop.
        return 404, {"detail": "unknown request id"}
    if job["status"] != "submitted":
        return 200, {"result": "ignored", "reason": f"job is {job['status']}"}  # repeated delivery
    error = body.get("error") or body.get("payload_error")
    if body.get("status") == "OK" and body.get("payload") is not None and not error:
        applied = ledger.vendor_completed(job["id"], body["payload"])
    else:
        applied = ledger.vendor_failed(job["id"], f"fal reported an error: {error or body.get('status')}")
    return 200, {"result": "applied" if applied else "ignored"}


@router.post("/webhooks/fal")
async def fal_webhook(request: Request):
    raw = await request.body()  # raw bytes before any JSON parsing
    try:
        await run_in_threadpool(falwebhook.verify, request.headers, raw)
    except falwebhook.WebhookRejected as exc:
        log.warning("fal webhook rejected: %s", exc.reason)
        return JSONResponse({"detail": exc.reason}, status_code=401)
    try:
        body = json.loads(raw)
        body["request_id"]
    except (ValueError, KeyError, TypeError):
        return JSONResponse({"detail": "body is not a fal webhook payload"}, status_code=400)
    status, content = await run_in_threadpool(handle_delivery, body)
    return JSONResponse(content, status_code=status)
