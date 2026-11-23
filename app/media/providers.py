"""Which vendor renders which video model, and at which quality (config rows video_vendors, video_failover, pilot_quality, pilot_draft_resolution).

The model keys (kling_v4, kling_v3_pro, seedance_2_0_i2v, seedance_2_0_ref) name the MODEL, whatever host runs it, so learned rules and camera
counters carry over when a model moves between Higgsfield and fal. The vendor is a separate setting.
"""
from __future__ import annotations

from app.config import get_config

FAL, HIGGSFIELD = "fal", "higgsfield"


def setting(key: str):
    """A config row, or the shipped default when this database was seeded before the row existed."""
    try:
        return get_config(key)
    except KeyError:
        from app.config_defaults import CONFIG_DEFAULTS

        return CONFIG_DEFAULTS[key][0]


def video_vendor(model_key: str) -> str:
    """'higgsfield' or 'fal' for a video model key. Models not listed (the avatar and both lip-sync engines) are fal only."""
    vendor = (setting("video_vendors") or {}).get(model_key, FAL)
    if vendor == HIGGSFIELD:
        from app.vendors import higgsfield

        if model_key not in higgsfield.ENDPOINT_BY_MODEL_KEY:   # a model Higgsfield does not offer cannot be routed there
            return FAL
    return vendor if vendor in (FAL, HIGGSFIELD) else FAL


def failover_enabled() -> bool:
    return bool(setting("video_failover"))


def pilot_quality() -> str:
    return "draft" if str(setting("pilot_quality")) == "draft" else "final"


def draft_resolution() -> str:
    return str(setting("pilot_draft_resolution"))


def clip_quality(batch: str, spec: dict | None) -> str:
    """'draft' for a pilot clip while pilot_quality is draft, else 'final'. A brand placement shot is always final: the pilot approval is where the
    client checks the real product, so the picture must be at full quality. A shot marked quality=final (re-rendered after approval) is final."""
    spec = spec or {}
    if batch != "pilot" or pilot_quality() != "draft" or spec.get("brand") or spec.get("quality") == "final":
        return "final"
    return "draft"
