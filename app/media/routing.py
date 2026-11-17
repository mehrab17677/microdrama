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


def tier_for(shot_type: str) -> str:
    return TIER_BY_TYPE[shot_type]


def needs_lipsync(tier: str, line_count: int) -> bool:
    """B2 and C shots that carry speech are lip-synced afterwards."""
    return tier in ("B2", "C") and line_count > 0
