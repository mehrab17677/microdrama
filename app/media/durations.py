"""Speech first, then picture: shot and episode durations (guide 10.2). Pure functions."""
from __future__ import annotations

import math

MIN_EST, MAX_EST = 2.0, 10.0
MAX_SHOT = 10.0
MIN_AUDIO = 2.0   # Kling audio limit (guide 10.3): a shorter B1 shot is stretched to 2 s
RUNTIME_RANGE = (75.0, 90.0)


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def planned_seconds(line_durations: list[float], est_seconds: float | None, timing: dict) -> float:
    """Seconds used in the cut. A shot with lines: head pad + speech + gaps + tail pad. Others: the
    director's estimate clamped to 2..10."""
    if line_durations:
        speech = sum(line_durations) + timing["line_gap"] * (len(line_durations) - 1)
        return timing["pad_head"] + speech + timing["pad_tail"]
    return clamp(est_seconds if est_seconds is not None else 4.0, MIN_EST, MAX_EST)


def render_seconds(tier: str, planned: float) -> int:
    """Seconds requested from the model. Minimum clip lengths apply (Seedance 4, Kling V3 3); the
    clip is trimmed back to `planned` in the compile step. B1 follows the audio length."""
    if tier in ("A", "B2"):
        return int(clamp(math.ceil(planned), 4, 10))
    if tier == "C":
        return int(clamp(math.ceil(planned), 3, 10))
    return int(math.ceil(planned))  # B1 and INSERT


def runtime_seconds(planned: list[float]) -> float:
    return sum(planned)


def rescale_nonspeaking(speaking_total: float, est: list[float]) -> list[float]:
    """Scale the non-speaking shots by one common factor, each kept within 2..10, to land the episode
    runtime inside 75..90 s. Returns the new planned seconds (unchanged if nothing helps)."""
    lo, hi = RUNTIME_RANGE

    def total(f: float) -> float:
        return speaking_total + sum(clamp(e * f, MIN_EST, MAX_EST) for e in est)

    current = total(1.0)
    if lo <= current <= hi or not est:
        return [clamp(e, MIN_EST, MAX_EST) for e in est]
    target = lo + 0.5 if current < lo else hi - 0.5   # aim just inside the range
    a, b = 0.01, 100.0
    if not total(a) <= target <= total(b):
        return [clamp(e, MIN_EST, MAX_EST) for e in est]  # cannot reach it
    for _ in range(60):
        mid = (a + b) / 2
        a, b = (mid, b) if total(mid) < target else (a, mid)
    f = (a + b) / 2
    return [clamp(e * f, MIN_EST, MAX_EST) for e in est]


def fit_episode(shots: list[dict], timing: dict) -> dict:
    """shots: [{idx, tier, line_durations: [...], est_seconds}] in order.
    Returns {planned: {idx: s}, render: {idx: int}, runtime, too_long: [idx], in_range: bool}."""
    planned = {s["idx"]: planned_seconds(s["line_durations"], s.get("est_seconds"), timing) for s in shots}
    for s in shots:  # a B1 shot's audio is the clip's audio, and the lip-sync/avatar endpoints need at least 2 s of it
        if s["tier"] == "B1" and s["line_durations"]:
            planned[s["idx"]] = max(planned[s["idx"]], MIN_AUDIO)
    too_long = [idx for idx, p in planned.items() if p > MAX_SHOT]
    runtime = runtime_seconds(list(planned.values()))
    lo, hi = RUNTIME_RANGE
    if not lo <= runtime <= hi:
        silent = [s for s in shots if not s["line_durations"] and not s.get("fixed")]   # a brand insert keeps the seconds that let each asset be read
        speaking_total = sum(p for idx, p in planned.items() if idx not in {s["idx"] for s in silent})
        scaled = rescale_nonspeaking(speaking_total, [planned[s["idx"]] for s in silent])
        for s, p in zip(silent, scaled):
            planned[s["idx"]] = p
        runtime = runtime_seconds(list(planned.values()))
    render = {s["idx"]: render_seconds(s["tier"], planned[s["idx"]]) for s in shots}
    return {"planned": planned, "render": render, "runtime": runtime, "too_long": too_long, "in_range": lo <= runtime <= hi}
