"""Shot routing (guide 10.1): which tier and model a shot type uses."""
TIER_BY_TYPE = {
    "non_speaking": "A",
    "speaking_closeup": "B1",
    "speaking_wider": "B2",
    "hero": "C",
    "static_insert": "INSERT",
}
MODEL_KEY_BY_TIER = {
    "A": "seedance_1_5_pro",
    "B1": "kling_avatar_v2_pro",
    "B2": "seedance_1_5_pro",
    "C": "kling_v3_pro",
}
FALLBACK_MODEL_KEY_BY_TIER = {  # guide 11.5, used when the main model has failed twice
    "A": "kling_v3_pro",
    "B1": "seedance_1_5_pro",   # the B2 method: silent clip, then lip-sync
    "B2": "kling_v3_pro",
    "C": "seedance_1_5_pro",
}


# Which tier's METHOD a fallback shot uses (guide 11.5): A -> Kling V3 with audio on; B1 -> the B2 method (Seedance silent clip,
# then lip-sync); B2 -> Kling V3 with audio off, then lip-sync; C -> Seedance, with lip-sync when it carries speech.
FALLBACK_METHOD = {"A": "C", "B1": "B2", "B2": "C"}


def tier_for(shot_type: str) -> str:
    return TIER_BY_TYPE[shot_type]


def method_tier(tier: str, fallback_used: bool = False, line_count: int = 0) -> str:
    """The tier whose rendering method is used: the shot's own, or the fallback route's after two failed attempts."""
    if not fallback_used or tier == "INSERT":
        return tier
    if tier == "C":
        return "B2" if line_count else "A"
    return FALLBACK_METHOD[tier]


def model_key(tier: str, fallback_used: bool = False, line_count: int = 0) -> str:
    return MODEL_KEY_BY_TIER[method_tier(tier, fallback_used, line_count)]


def needs_lipsync(tier: str, line_count: int, fallback_used: bool = False) -> bool:
    """B2 and C shots that carry speech are lip-synced afterwards (a B1 shot too once it uses the B2 fallback method)."""
    return method_tier(tier, fallback_used, line_count) in ("B2", "C") and line_count > 0
