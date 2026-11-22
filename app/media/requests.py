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
    if camera_fixed and "camera" not in prompt.lower():   # neither Kling V3 nor Seedance 2.0 has a camera-lock switch: say it in the prompt
        prompt = prompt.rstrip() + " The camera does not move."
    if tier in ("A", "B2", "C"):    # Kling V3 Pro image-to-video: start_image_url, duration "3" to "15"; native audio only for a shot with no speech
        return {"endpoint": falmedia.KLING_V3, "arguments": {
            "prompt": prompt, "start_image_key": shot["keyframe_r2_key"], "duration": str(shot["render_duration"]),
            "generate_audio": shot["line_count"] == 0}}
    if tier == "S":                 # the fallback: Seedance 2.0 image-to-video (image_url, resolution, duration "4" to "15", aspect_ratio, generate_audio, seed)
        return {"endpoint": falmedia.SEEDANCE2_I2V, "arguments": {
            "prompt": prompt, "image_key": shot["keyframe_r2_key"], "resolution": "1080p", "duration": str(shot["render_duration"]),
            "aspect_ratio": "9:16", "generate_audio": shot["line_count"] == 0, "seed": stable_seed(shot["id"], shot["attempts"])}}
    if tier == "B1":
        return {"endpoint": falmedia.KLING_AVATAR, "arguments": {"image_key": shot["keyframe_r2_key"], "audio_key": shot_audio_key, "prompt": prompt}}
    raise ValueError(f"tier {tier} has no video request")


def brand_video_request(image_keys: list[str], prompt: str, render_duration: int, audio: bool) -> dict:
    """bytedance/seedance-2.0/reference-to-video (fal): image_urls (up to 9, we send 3 to 5), 1080p, 9:16, high bitrate. The keyframe is @Image1."""
    if not 1 <= len(image_keys) <= 9:
        raise ValueError("a brand shot needs 1 to 9 reference images")
    return {"endpoint": falmedia.SEEDANCE2_REF, "arguments": {"prompt": prompt, "image_keys": image_keys, "resolution": "1080p", "duration": str(render_duration),
                                                               "aspect_ratio": "9:16", "generate_audio": audio, "bitrate_mode": "high"}}


def lipsync_request(raw_clip_key: str, shot_audio_key: str, engine: str = "kling", seed_from=None) -> dict:
    if engine == "latentsync":   # fal-ai/latentsync: video_url, audio_url, guidance_scale, seed, loop_mode (fal docs)
        return {"endpoint": falmedia.LATENTSYNC, "arguments": {"video_key": raw_clip_key, "audio_key": shot_audio_key, "guidance_scale": 1.0,
                                                                 "seed": stable_seed(seed_from or raw_clip_key, "latentsync")}}
    return {"endpoint": falmedia.KLING_LIPSYNC, "arguments": {"video_key": raw_clip_key, "audio_key": shot_audio_key}}


def resolve_keys(arguments: dict) -> dict:
    """Presign each *_key argument at submit time (never store the URLs)."""
    out = {}
    for name, value in arguments.items():
        if name == "image_keys":
            out["image_urls"] = [storage.presign_get(k) for k in value]
        elif name in _KEY_TO_URL:
            out[_KEY_TO_URL[name]] = storage.presign_get(value)
        else:
            out[name] = value
    return out
