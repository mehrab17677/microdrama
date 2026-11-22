"""Shot routing (guide 10.1): which tier and model a shot type uses."""
TIER_BY_TYPE = {
    "non_speaking": "A",
    "speaking_closeup": "B1",
    "speaking_wider": "B2",
    "hero": "C",
    "static_insert": "INSERT",
}
# Seedance 1.5 Pro (the old model for tiers A and B2) was removed from fal on 11 Nov 2026. Tiers A, B2 and C now all use Kling V3 Pro (audio on for a shot
# with no speech, off for a shot that is lip-synced afterwards); method "S", Seedance 2.0 image-to-video at 1080p, is the fallback after two failed attempts.
MODEL_KEY_BY_TIER = {
    "A": "kling_v3_pro",
    "B1": "kling_avatar_v2_pro",
    "B2": "kling_v3_pro",
    "C": "kling_v3_pro",
    "S": "seedance_2_0_i2v",
}
BRAND_MODEL_KEY = "seedance_2_0_ref"   # brand placement shots: Seedance 2.0 reference-to-video with the real product pictures
FALLBACK_MODEL_KEY_BY_TIER = {  # guide 11.5, used when the main model has failed twice
    "A": "seedance_2_0_i2v",
    "B1": "kling_v3_pro",       # the B2 method: silent clip, then lip-sync
    "B2": "seedance_2_0_i2v",
    "C": "seedance_2_0_i2v",
}


# Which METHOD a fallback shot uses (guide 11.5): A, B2 and C -> S (Seedance 2.0 image-to-video, with lip-sync afterwards when the shot carries speech);
# B1 -> the B2 method (a silent Kling V3 clip, then lip-sync).
FALLBACK_METHOD = {"A": "S", "B1": "B2", "B2": "S", "C": "S"}


def tier_for(shot_type: str) -> str:
    return TIER_BY_TYPE[shot_type]


def method_tier(tier: str, fallback_used: bool = False, line_count: int = 0) -> str:
    """The tier whose rendering method is used: the shot's own, or the fallback route's after two failed attempts."""
    if not fallback_used or tier == "INSERT":
        return tier
    return FALLBACK_METHOD[tier]


def is_brand(spec: dict | None) -> bool:
    return bool((spec or {}).get("brand"))


def model_key(tier: str, fallback_used: bool = False, line_count: int = 0, brand: bool = False) -> str:
    """A brand shot is always Seedance 2.0 reference-to-video (there is no fallback model: a different model would be a different, unapproved look)."""
    if brand and tier in ("A", "B2"):
        return BRAND_MODEL_KEY
    return MODEL_KEY_BY_TIER[method_tier(tier, fallback_used, line_count)]


def shot_model_key(shot: dict) -> str:
    return model_key(shot["tier"], shot.get("fallback_used", False), shot.get("line_count", 0) or len(shot.get("line_ids") or []), is_brand(shot.get("spec")))


def needs_lipsync(tier: str, line_count: int, fallback_used: bool = False) -> bool:
    """B2, C and S shots that carry speech are lip-synced afterwards (a B1 shot too once it uses the B2 fallback method)."""
    return method_tier(tier, fallback_used, line_count) in ("B2", "C", "S") and line_count > 0


def lipsync_engine(sync_attempts: int, kling_failures: int, fallback: str = "latentsync") -> str:
    """'kling' or 'latentsync'. Kling LipSync is the main engine. The fallback (config lipsync_fallback) is used for the redo after a lip-sync
    check failed (sync_attempts >= 1) and for any retry after Kling itself failed or refused the clip."""
    if fallback == "latentsync" and (sync_attempts >= 1 or kling_failures >= 1):
        return "latentsync"
    return "kling"
