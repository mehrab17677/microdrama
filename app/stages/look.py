"""Look-stage planning (guide 9.5): which pack images a character needs. Pure functions."""
from __future__ import annotations

TURNAROUND_VIEWS = ["front", "three_quarter_left", "three_quarter_right", "profile", "back"]
WARDROBE_VIEWS = ["full_body_front", "three_quarter"]
POSES = ["standing", "sitting", "walking", "arms_crossed"]
MAX_EXPRESSIONS_LEAD = 8
EXPRESSIONS_OTHER = 3


def pack_plan(is_lead: bool, outfit_codes: list[str], emotion_groups: list[str]) -> list[dict]:
    """The pack images for one character: [{kind, tags}]. Leads get turnaround, up to 8 expressions,
    two wardrobe views per outfit and four poses. Other characters get 3 expressions and one wardrobe
    image per outfit. The cast line-up is one image for everyone and is planned elsewhere."""
    plan: list[dict] = []
    groups = list(dict.fromkeys(emotion_groups))
    if is_lead:
        plan += [{"kind": "turnaround", "tags": {"angle": a}} for a in TURNAROUND_VIEWS]
        plan += [{"kind": "expression", "tags": {"emotion_group": g}} for g in groups[:MAX_EXPRESSIONS_LEAD]]
        plan += [{"kind": "wardrobe", "tags": {"outfit": o, "view": v}} for o in outfit_codes for v in WARDROBE_VIEWS]
        plan += [{"kind": "pose", "tags": {"pose": p}} for p in POSES]
    else:
        plan += [{"kind": "expression", "tags": {"emotion_group": g}} for g in groups[:EXPRESSIONS_OTHER]]
        plan += [{"kind": "wardrobe", "tags": {"outfit": o, "view": WARDROBE_VIEWS[0]}} for o in outfit_codes]
    return plan
