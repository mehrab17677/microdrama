"""Video on Higgsfield (Kling 4.0, Kling 3.0 Pro, Seedance 2.0). REST client: submit, poll, estimate.

Docs: https://docs.higgsfield.ai  (checked 23 Nov 2026, see docs/VENDOR_NOTES.md). Submit with POST https://api.higgsfield.ai/{model_id}, then poll the
returned status_url until it is `completed`, `failed`, `nsfw` or `canceled`. The finished request has `video.url` (the same shape as fal).
Credentials: HIGGSFIELD_API_KEY = "KEY_ID:KEY_SECRET", sent as `Authorization: Key KEY_ID:KEY_SECRET`. Every input must be a public https URL (data URIs and asset:// are rejected).
"""
from __future__ import annotations

import os
import uuid

import httpx

from app.config import dry_run, get_config
from app.jobs.errors import PermanentError, TransientError
from app.vendors import fixtures
from app.vendors.errors import classify_status

BASE = "https://api.higgsfield.ai"

KLING_V4 = "kling-video/v4.0/image-to-video"                 # tier A: first frame, 3 to 30 s, 720p or 1080p; no aspect_ratio field (the frame decides)
KLING_V3_PRO = "kling-video/v3.0/pro/image-to-video"         # tiers B2 and C: 1080p, 3 to 15 s, sound on or off
KLING_V3_STD = "kling-video/v3.0/std/image-to-video"         # the 720p draft of the above (the pilot before approval)
SEEDANCE2_I2V = "bytedance/seedance-2.0/image-to-video"      # the fallback "S": 480p to 4k, 4 to 15 s
SEEDANCE2_REF = "bytedance/seedance-2.0/reference-to-video"  # brand shots: up to 9 reference images

ENDPOINT_BY_MODEL_KEY = {
    "kling_v4": KLING_V4,
    "kling_v3_pro": KLING_V3_PRO,
    "seedance_2_0_i2v": SEEDANCE2_I2V,
    "seedance_2_0_ref": SEEDANCE2_REF,
}
DRAFT_ENDPOINT_BY_MODEL_KEY = {"kling_v3_pro": KLING_V3_STD}   # a model with no cheaper tier renders the draft with a lower resolution setting instead


class OutOfCredit(PermanentError):
    """The Higgsfield balance is empty (HTTP 403 not_enough_credits). Retrying cannot help; the clip handler fails over to fal."""


class Unavailable(TransientError):
    """Higgsfield is down, rate limited or its queue is full."""


def _headers(extra: dict | None = None) -> dict:
    h = {"Content-Type": "application/json", **(extra or {})}
    key = os.environ.get("HIGGSFIELD_API_KEY") or os.environ.get("HF_KEY")
    if key:                        # with the key held by a proxy no variable is set and the proxy adds the header
        h["Authorization"] = f"Key {key}"
    return h


def _detail(resp: httpx.Response) -> str:
    try:
        d = resp.json().get("detail")
        return d if isinstance(d, str) else str(d or resp.text)[:300]
    except Exception:  # noqa: BLE001
        return resp.text[:300]


def _raise_for(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    detail = _detail(resp)
    if resp.status_code in (401, 402) or "not_enough_credits" in detail or "insufficient" in detail.lower():
        raise OutOfCredit(f"Higgsfield: {detail or resp.status_code} (add credit in the Higgsfield console)")
    if resp.status_code == 429 or resp.status_code >= 500:
        raise Unavailable(f"Higgsfield HTTP {resp.status_code}: {detail}")
    err = classify_status(resp.status_code, detail)
    raise err or PermanentError(f"Higgsfield HTTP {resp.status_code}: {detail}")


def _call(method: str, url: str, body: dict | None = None, headers: dict | None = None) -> httpx.Response:
    try:
        return httpx.request(method, url, json=body, headers=_headers(headers), timeout=60)
    except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as exc:
        raise Unavailable(f"Higgsfield: {type(exc).__name__}: {exc}") from exc


# ---------- cost ----------

def estimate_cost(model_key: str, *, duration: float = 0, audio: bool = False, quality: str = "final") -> float:
    """USD for one clip. Prices are Higgsfield's self-serve list prices per output second (price_book video.hf_*)."""
    from app.config_defaults import CONFIG_DEFAULTS

    video = {**CONFIG_DEFAULTS["price_book"][0]["video"], **get_config("price_book")["video"]}
    draft = quality == "draft"
    if model_key == "kling_v4":
        p = video["hf_kling_v4"]
        return round(duration * p["per_s"][p["draft_resolution"] if draft else p["resolution"]], 6)
    if model_key == "kling_v3_pro":
        p = video["hf_kling_v3_std" if draft else "hf_kling_v3_pro"]
        return round(duration * (p["per_s_audio"] if audio else p["per_s_silent"]), 6)
    if model_key in ("seedance_2_0_i2v", "seedance_2_0_ref"):
        p = video["hf_" + model_key]
        return round(duration * p["per_s"][p["draft_resolution"] if draft else p["resolution"]], 6)
    raise PermanentError(f"no Higgsfield price for video model key {model_key!r}")


def estimate(endpoint: str, arguments: dict) -> dict:
    """Higgsfield's own estimate for a request (free, nothing is generated). Used by the smoke script; the live price is the authority."""
    resp = _call("POST", f"{BASE}/estimate/{endpoint}", arguments)
    _raise_for(resp)
    return resp.json()


# ---------- queue ----------

def submit(endpoint: str, arguments: dict, idempotency_key: str | None = None) -> str:
    """Submit a generation and return Higgsfield's request id. The same Idempotency-Key makes a retried submission safe."""
    if dry_run():
        return "dry-hf-" + uuid.uuid4().hex
    resp = _call("POST", f"{BASE}/{endpoint}", arguments, {"Idempotency-Key": idempotency_key or str(uuid.uuid4())})
    _raise_for(resp)
    body = resp.json()
    if not body.get("request_id"):
        raise PermanentError(f"Higgsfield returned no request_id: {str(body)[:200]}")
    return body["request_id"]


def poll_status(job: dict) -> tuple[str, object]:
    """For the poller: ('pending', None) | ('done', payload) | ('error', message)."""
    if dry_run():
        return "done", {"dry_run": True, "video": {"url": fixtures.fixture_path("clip_4s_1080x1920.mp4").as_uri()}}
    resp = _call("GET", f"{BASE}/requests/{job['vendor_request_id']}/status")
    _raise_for(resp)
    body = resp.json()
    state = body.get("status")
    if state in ("queued", "in_progress", None):
        return "pending", None
    if state == "completed":
        return "done", body
    if state == "nsfw":
        return "error", "nsfw: Higgsfield's content check blocked this clip"
    return "error", f"{state}: {body.get('error') or body.get('detail') or 'no detail'}"


def upload(data: bytes, content_type: str = "image/png") -> str:
    """Give Higgsfield a file it can read: a presigned upload, then the public URL to use as an input (jpeg, png, webp, gif, wav, mp4; kept 7 days).
    The app itself sends presigned R2 URLs; this is for the smoke test and for anyone without a public bucket. Never send the Higgsfield key to the upload URL."""
    if dry_run():
        return "https://example.com/dry-run-upload"
    resp = _call("POST", f"{BASE}/files/generate-upload-url", {"content_type": content_type})
    _raise_for(resp)
    body = resp.json()
    put = httpx.put(body["upload_url"], content=data, headers=body.get("upload_headers") or {"Content-Type": content_type}, timeout=120)
    if put.status_code >= 300:
        raise PermanentError(f"Higgsfield upload failed: HTTP {put.status_code}")
    return body["public_url"]


def cancel(request_id: str) -> None:
    if dry_run():
        return
    _call("POST", f"{BASE}/requests/{request_id}/cancel")
