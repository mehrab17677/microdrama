"""Video and lip-sync (guide 6.4), fal. Client: fal_client, key in FAL_KEY.

Submit through the queue with a webhook; the poller covers a missing webhook. A queue status of
COMPLETED also covers failures: the Completed status then carries `error` / `error_type`.
"""
from __future__ import annotations

import math
import os
import uuid
from pathlib import Path

import httpx

from app.config import dry_run, get_config, get_settings
from app.jobs.errors import PermanentError
from app.vendors import fixtures
from app.vendors.errors import map_exception

SEEDANCE = "fal-ai/bytedance/seedance/v1.5/pro/image-to-video"
KLING_V3 = "fal-ai/kling-video/v3/pro/image-to-video"
KLING_AVATAR = "fal-ai/kling-video/ai-avatar/v2/pro"
KLING_LIPSYNC = "fal-ai/kling-video/lipsync/audio-to-video"
SEEDANCE2_REF = "bytedance/seedance-2.0/reference-to-video"   # brand shots: up to 9 reference images, @Image1 tags (fal docs)
LATENTSYNC = "fal-ai/latentsync"   # open-source LatentSync hosted on fal: the lip-sync fallback (docs/LIPSYNC_OPTIONS.md)
LATENTSYNC_MAX_SECONDS = 40

ENDPOINT_BY_MODEL_KEY = {
    "seedance_1_5_pro": SEEDANCE,
    "kling_v3_pro": KLING_V3,
    "kling_avatar_v2_pro": KLING_AVATAR,
    "kling_lipsync": KLING_LIPSYNC,
    "latentsync": LATENTSYNC,
    "seedance_2_0_ref": SEEDANCE2_REF,
}


def webhook_url() -> str | None:
    base = get_settings().public_base_url
    return base.rstrip("/") + "/webhooks/fal" if base else None


# ---------- cost (price formulas; fal has no estimate endpoint) ----------

def estimate_cost(model_key: str, *, duration: float = 0, audio: bool = False, width: int = 1080, height: int = 1920, fps: float | None = None) -> float:
    """USD. `duration` is the rendered seconds (video seconds; audio seconds for the avatar)."""
    video = get_config("price_book")["video"]
    if model_key == "seedance_1_5_pro":
        p = video["seedance_1_5_pro"]
        tokens = height * width * (fps or get_config("seedance_fps")) * duration / 1024
        return round(tokens / 1e6 * (p["per_m_tokens_audio"] if audio else p["per_m_tokens_silent"]), 6)
    if model_key == "kling_v3_pro":
        p = video["kling_v3_pro"]
        return round(duration * (p["per_s_audio"] if audio else p["per_s_silent"]), 6)
    if model_key == "kling_avatar_v2_pro":
        return round(duration * video["kling_avatar_v2_pro"]["per_audio_s"], 6)
    if model_key == "kling_lipsync":
        p = video["kling_lipsync"]
        return round(math.ceil(duration / p["step_s"]) * p["step_s"] * p["per_s"], 6)
    if model_key == "seedance_2_0_ref":   # per generated second, by resolution (fal, Nov 2026); audio costs the same on or off
        p = video["seedance_2_0_ref"]
        return round(duration * p["per_s"][p["resolution"]], 6)
    if model_key == "latentsync":   # a flat price for a video up to 40 s, then per second
        p = video["latentsync"]
        return round(p["flat"] if duration <= p["flat_up_to_s"] else duration * p["per_s_over"], 6)
    raise PermanentError(f"unknown video model key {model_key!r}")


def check_latentsync_limits(*, video_seconds: float, audio_seconds: float) -> None:
    problems = []
    if video_seconds > LATENTSYNC_MAX_SECONDS:
        problems.append(f"video is {video_seconds:.1f} s; the LatentSync endpoint is priced up to {LATENTSYNC_MAX_SECONDS} s")
    if abs(audio_seconds - video_seconds) > 0.25:
        problems.append("audio and video lengths must match")
    if problems:
        raise PermanentError("; ".join(problems))


def check_lipsync_limits(*, video_seconds: float, video_bytes: int, width: int, height: int, audio_seconds: float, audio_bytes: int) -> None:
    """Validate locally before submitting: fal can bill a request rejected for bad input."""
    problems = []
    if not 2 <= video_seconds <= 10:
        problems.append(f"video is {video_seconds:.2f} s; lip-sync needs 2 to 10 s")
    if video_bytes > 100 * 1024 * 1024:
        problems.append("video is over 100 MB")
    if not (720 <= width <= 1920 and 720 <= height <= 1920):
        problems.append(f"video is {width}x{height}; lip-sync needs both sides 720 to 1920 px")
    if not 2 <= audio_seconds <= 60:
        problems.append(f"audio is {audio_seconds:.2f} s; lip-sync needs 2 to 60 s")
    if audio_bytes > 5 * 1024 * 1024:
        problems.append("audio is over 5 MB")
    if problems:
        raise PermanentError("; ".join(problems))


# ---------- queue ----------

def submit(endpoint_id: str, arguments: dict) -> str:
    """Submit a job and return fal's request id."""
    if dry_run():
        return "dry-" + uuid.uuid4().hex
    import fal_client

    try:
        handle = fal_client.submit(endpoint_id, arguments=arguments, webhook_url=webhook_url())
    except Exception as exc:  # noqa: BLE001
        raise map_exception(exc) from exc
    return handle.request_id


def poll_status(job: dict) -> tuple[str, object]:
    """For the poller: ('pending', None) | ('done', payload) | ('error', message)."""
    if dry_run():
        return "done", {"dry_run": True, "video": {"url": fixtures.fixture_path("clip_4s_1080x1920.mp4").as_uri()}}
    import fal_client

    endpoint, request_id = job["endpoint"], job["vendor_request_id"]
    try:
        status = fal_client.status(endpoint, request_id)
        if not isinstance(status, fal_client.Completed):
            return "pending", None
        if status.error or status.error_type:
            return "error", f"{status.error_type or 'error'}: {status.error}"
        return "done", fal_client.result(endpoint, request_id)
    except fal_client.FalClientHTTPError as exc:
        if 400 <= exc.status_code < 500:
            return "error", f"HTTP {exc.status_code}: {exc.response_text if hasattr(exc, 'response_text') else exc}"
        raise  # 5xx: let the poller try again on its next tick
    except Exception as exc:  # noqa: BLE001
        raise map_exception(exc) from exc


def video_url(payload: dict) -> str:
    try:
        return payload["video"]["url"]
    except (KeyError, TypeError):
        raise PermanentError(f"fal payload has no video.url: {str(payload)[:200]}") from None


def download(url: str, dest: str | Path) -> Path:
    """Download a vendor output. Copy it to R2 straight away: vendor URLs expire."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if url.startswith("file://"):
        dest.write_bytes(Path(httpx.URL(url).path).read_bytes())
        return dest
    try:
        with httpx.stream("GET", url, timeout=300, follow_redirects=True) as resp:
            if resp.status_code >= 400:
                raise map_exception(httpx.HTTPStatusError("download failed", request=resp.request, response=resp))
            with open(dest, "wb") as fh:
                for chunk in resp.iter_bytes():
                    fh.write(chunk)
    except Exception as exc:  # noqa: BLE001
        raise map_exception(exc) from exc
    return dest
