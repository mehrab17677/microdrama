"""Estimated video cost of a shot, one attempt (guide 6.4 formulas, 7.5 budget)."""
from __future__ import annotations

from app.media.routing import MODEL_KEY_BY_TIER, method_tier, needs_lipsync
from app.vendors import falmedia


def shot_clip_cost(tier: str, planned: float, render: int, line_count: int, fallback_used: bool = False, brand: bool = False) -> float:
    if brand and tier in ("A", "B2"):
        return falmedia.estimate_cost("seedance_2_0_ref", duration=render)
    tier = method_tier(tier, fallback_used, line_count)
    if tier == "INSERT":
        return 0.0
    key = MODEL_KEY_BY_TIER[tier]
    if tier == "B1":
        return falmedia.estimate_cost(key, duration=planned)                       # audio seconds
    if tier == "A":
        return falmedia.estimate_cost(key, duration=render, audio=True)
    if tier == "B2":
        return falmedia.estimate_cost(key, duration=render, audio=False)
    return falmedia.estimate_cost(key, duration=render, audio=(line_count == 0))   # C


def shot_lipsync_cost(tier: str, line_count: int, render: int, fallback_used: bool = False, engine: str = "kling") -> float:
    if not needs_lipsync(tier, line_count, fallback_used):
        return 0.0
    return falmedia.estimate_cost("latentsync" if engine == "latentsync" else "kling_lipsync", duration=render)
