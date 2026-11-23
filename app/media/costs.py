"""Estimated video cost of a shot, one attempt (guide 6.4 formulas, 7.5 budget). Prices depend on the vendor that renders the model (app/media/providers.py)."""
from __future__ import annotations

from app.media import providers
from app.media.routing import MODEL_KEY_BY_TIER, method_tier, needs_lipsync
from app.vendors import falmedia, higgsfield


def clip_price(model_key: str, *, duration: float, audio: bool = False, quality: str = "final", vendor: str | None = None) -> float:
    """USD for one clip of `duration` seconds on the vendor that runs `model_key` (or the given vendor, as after a failover)."""
    vendor = vendor or providers.video_vendor(model_key)
    if vendor == "higgsfield" and model_key in higgsfield.ENDPOINT_BY_MODEL_KEY:
        return higgsfield.estimate_cost(model_key, duration=duration, audio=audio, quality=quality)
    if model_key == "kling_v4":            # on fal tier A is Kling V3 Pro
        model_key = "kling_v3_pro"
    return falmedia.estimate_cost(model_key, duration=duration, audio=audio)


def shot_clip_cost(tier: str, planned: float, render: int, line_count: int, fallback_used: bool = False, brand: bool = False, quality: str = "final") -> float:
    if brand and tier in ("A", "B2"):
        return clip_price("seedance_2_0_ref", duration=render)      # a brand shot is always final quality
    tier = method_tier(tier, fallback_used, line_count)
    if tier == "INSERT":
        return 0.0
    key = MODEL_KEY_BY_TIER[tier]
    if tier == "B1":
        return falmedia.estimate_cost(key, duration=planned)                       # audio seconds (fal)
    if tier == "S":
        return clip_price(key, duration=render, quality=quality)                   # Seedance 2.0: one price per second, audio included
    return clip_price(key, duration=render, audio=(line_count == 0), quality=quality)   # Kling: native sound only when nobody speaks


def shot_lipsync_cost(tier: str, line_count: int, render: int, fallback_used: bool = False, engine: str = "kling") -> float:
    if not needs_lipsync(tier, line_count, fallback_used):
        return 0.0
    return falmedia.estimate_cost("latentsync" if engine == "latentsync" else "kling_lipsync", duration=render)
