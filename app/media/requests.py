"""Request builders for the video and lip-sync endpoints (guide 10.4).

R2 keys are stored in the job request; the handler replaces each *_key with a presigned URL when it submits.
"""
from __future__ import annotations

import hashlib

from app.vendors import falmedia, storage

_KEY_TO_URL = {"image_key": "image_url", "start_image_key": "start_image_url", "audio_key": "audio_url", "video_key": "video_url"}


def stable_seed(shot_id, attempt) -> int:
    """First 8 hex digits of sha256(shot_id + attempt), as an integer."""
    return int(hashlib.sha256(f"{shot_id}{attempt}".encode()).hexdigest()[:8], 16)


def video_request(tier: str, shot: dict, prompt: str, camera_fixed: bool, shot_audio_key: str | None = None) -> dict:
    """shot: {id, keyframe_r2_key, render_duration, attempts, line_count}."""
    if tier in ("A", "B2"):
        return {"endpoint": falmedia.SEEDANCE, "arguments": {
            "prompt": prompt, "image_key": shot["keyframe_r2_key"], "resolution": "1080p", "duration": str(shot["render_duration"]),
            "aspect_ratio": "9:16", "generate_audio": tier == "A", "camera_fixed": camera_fixed, "seed": stable_seed(shot["id"], shot["attempts"])}}
    if tier == "C":
        return {"endpoint": falmedia.KLING_V3, "arguments": {
            "prompt": prompt, "start_image_key": shot["keyframe_r2_key"], "duration": str(shot["render_duration"]),
            "generate_audio": shot["line_count"] == 0}}
    if tier == "B1":
        return {"endpoint": falmedia.KLING_AVATAR, "arguments": {"image_key": shot["keyframe_r2_key"], "audio_key": shot_audio_key, "prompt": prompt}}
    raise ValueError(f"tier {tier} has no video request")


def lipsync_request(raw_clip_key: str, shot_audio_key: str) -> dict:
    return {"endpoint": falmedia.KLING_LIPSYNC, "arguments": {"video_key": raw_clip_key, "audio_key": shot_audio_key}}


def resolve_keys(arguments: dict) -> dict:
    """Presign each *_key argument at submit time (never store the URLs)."""
    out = {}
    for name, value in arguments.items():
        if name in _KEY_TO_URL:
            out[_KEY_TO_URL[name]] = storage.presign_get(value)
        else:
            out[name] = value
    return out
