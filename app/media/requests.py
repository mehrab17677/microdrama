"""Request builders for the video and lip-sync endpoints (guide 10.4).

R2 keys are stored in the job request; the handler replaces each *_key with a presigned URL when it submits.
"""
from __future__ import annotations

import hashlib

from app.media import providers
from app.media.routing import MODEL_KEY_BY_TIER
from app.vendors import falmedia, higgsfield, storage

_KEY_TO_URL = {"image_key": "image_url", "start_image_key": "start_image_url", "audio_key": "audio_url", "video_key": "video_url"}


def stable_seed(shot_id, attempt) -> int:
    """First 8 hex digits of sha256(shot_id + attempt), as an integer."""
    return int(hashlib.sha256(f"{shot_id}{attempt}".encode()).hexdigest()[:8], 16)


def _fal_clip(model_key: str, shot: dict, prompt: str, quality: str) -> dict:
    """The fal request for a clip. Kling 4.0 is not used on fal: tier A there is Kling V3 Pro (the failover model)."""
    if model_key in ("kling_v4", "kling_v3_pro"):    # fal Kling V3 Pro image-to-video: start_image_url, duration "3" to "15"; native audio only for a shot with no speech
        return {"endpoint": falmedia.KLING_V3, "model_key": "kling_v3_pro", "arguments": {
            "prompt": prompt, "start_image_key": shot["keyframe_r2_key"], "duration": str(shot["render_duration"]),
            "generate_audio": shot["line_count"] == 0}}
    return {"endpoint": falmedia.SEEDANCE2_I2V, "model_key": "seedance_2_0_i2v", "arguments": {   # Seedance 2.0 image-to-video (fal): image_url, resolution, duration "4" to "15", aspect_ratio, generate_audio, seed
        "prompt": prompt, "image_key": shot["keyframe_r2_key"], "resolution": providers.draft_resolution() if quality == "draft" else "1080p",
        "duration": str(shot["render_duration"]), "aspect_ratio": "9:16", "generate_audio": shot["line_count"] == 0, "seed": stable_seed(shot["id"], shot["attempts"])}}


def _hf_clip(model_key: str, shot: dict, prompt: str, quality: str) -> dict:
    """The Higgsfield request for a clip (field names from docs.higgsfield.ai, 23 Nov 2026)."""
    draft = quality == "draft"
    seconds = int(shot["render_duration"])
    if model_key == "kling_v4":      # first frame, prompt, duration 3 to 30, resolution 720p or 1080p. No aspect_ratio and no sound switch: the frame decides the shape
        return {"endpoint": higgsfield.KLING_V4, "model_key": model_key, "arguments": {
            "prompt": prompt, "image_key": shot["keyframe_r2_key"], "duration": seconds, "resolution": "720p" if draft else "1080p"}}
    if model_key == "kling_v3_pro":  # image_url, prompt (2,500 characters at most), duration 3 to 15, sound on or off; the Standard endpoint is the 720p draft
        return {"endpoint": higgsfield.DRAFT_ENDPOINT_BY_MODEL_KEY["kling_v3_pro"] if draft else higgsfield.KLING_V3_PRO, "model_key": model_key, "arguments": {
            "prompt": prompt[:2500], "image_key": shot["keyframe_r2_key"], "duration": seconds, "sound": "on" if shot["line_count"] == 0 else "off"}}
    return {"endpoint": higgsfield.SEEDANCE2_I2V, "model_key": "seedance_2_0_i2v", "arguments": {   # image_url, prompt, duration 4 to 15, resolution, generate_audio; there is no seed field
        "prompt": prompt, "image_key": shot["keyframe_r2_key"], "duration": seconds, "resolution": providers.draft_resolution() if draft else "1080p",
        "generate_audio": shot["line_count"] == 0}}


def video_request(tier: str, shot: dict, prompt: str, camera_fixed: bool, shot_audio_key: str | None = None, *, vendor: str = "fal", quality: str = "final") -> dict:
    """shot: {id, keyframe_r2_key, render_duration, attempts, line_count}. Returns {endpoint, arguments, model_key, vendor, quality, failover}.
    `failover` is the same clip as a fal request when the vendor is Higgsfield (used if Higgsfield has no credit or is down), else None."""
    if camera_fixed and "camera" not in prompt.lower():   # none of these models has a camera-lock switch: say it in the prompt
        prompt = prompt.rstrip() + " The camera does not move."
    if tier == "B1":
        return {"endpoint": falmedia.KLING_AVATAR, "model_key": "kling_avatar_v2_pro", "vendor": "fal", "quality": "final", "failover": None,
                "arguments": {"image_key": shot["keyframe_r2_key"], "audio_key": shot_audio_key, "prompt": prompt}}
    if tier not in ("A", "B2", "C", "S"):
        raise ValueError(f"tier {tier} has no video request")
    model_key = MODEL_KEY_BY_TIER[tier]
    fal_req = _fal_clip(model_key, shot, prompt, quality)
    if vendor == "higgsfield":
        req = _hf_clip(model_key, shot, prompt, quality)
        return {**req, "vendor": "higgsfield", "quality": quality, "failover": {k: fal_req[k] for k in ("endpoint", "arguments", "model_key")}}
    return {**fal_req, "vendor": "fal", "quality": quality, "failover": None}


def brand_video_request(image_keys: list[str], prompt: str, render_duration: int, audio: bool, *, vendor: str = "fal") -> dict:
    """Seedance 2.0 reference-to-video with the real product pictures (up to 9; we send 3 to 5), 1080p, 9:16. The keyframe is @Image1.
    fal: bitrate_mode high. Higgsfield: no bitrate field, integer duration."""
    if not 1 <= len(image_keys) <= 9:
        raise ValueError("a brand shot needs 1 to 9 reference images")
    fal_args = {"prompt": prompt, "image_keys": image_keys, "resolution": "1080p", "duration": str(render_duration), "aspect_ratio": "9:16", "generate_audio": audio, "bitrate_mode": "high"}
    fal_req = {"endpoint": falmedia.SEEDANCE2_REF, "arguments": fal_args, "model_key": "seedance_2_0_ref"}
    if vendor == "higgsfield":
        args = {"prompt": prompt, "image_keys": image_keys, "resolution": "1080p", "duration": int(render_duration), "aspect_ratio": "9:16", "generate_audio": audio}
        return {"endpoint": higgsfield.SEEDANCE2_REF, "arguments": args, "model_key": "seedance_2_0_ref", "vendor": "higgsfield", "quality": "final", "failover": fal_req}
    return {**fal_req, "vendor": "fal", "quality": "final", "failover": None}


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
