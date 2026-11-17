"""Lip-sync scoring (guide 11.2).

class LipSyncScorer(Protocol):
    def score(self, video_path) -> dict | None   # {'confidence': float, 'offset_frames': int}, or None if it cannot score

This component is UNVERIFIED. The public SyncNet reference code is MIT, but the licence of its pretrained weights is not
stated, so commercial use cannot be confirmed (docs/VENDOR_NOTES.md). Until the owner clears a scorer, NullScorer is
shipped: it always returns None, the check is recorded as failed with category 'unscored', and unscored shots are
never auto-repaired. They are listed for the reviewer, and lip-sync is judged by people at Gates 4 and 5.
To add a real scorer: implement `score` (a subprocess wrapper is recommended), then register it with set_scorer().
"""
from __future__ import annotations

from typing import Protocol


class LipSyncScorer(Protocol):
    name: str

    def score(self, video_path: str) -> dict | None: ...


class NullScorer:
    name = "none"

    def score(self, video_path: str) -> dict | None:
        return None


_scorer: LipSyncScorer = NullScorer()


def get_scorer() -> LipSyncScorer:
    return _scorer


def set_scorer(scorer: LipSyncScorer) -> None:
    global _scorer
    _scorer = scorer


def judge(result: dict | None, conf_min: float, offset_max: int) -> tuple[bool, str | None, float | None, dict]:
    """(passed, category, score, detail). None is recorded as failed with category 'unscored'."""
    if result is None:
        return False, "unscored", None, {"scorer": get_scorer().name}
    conf, offset = float(result["confidence"]), int(result["offset_frames"])
    ok = conf >= conf_min and abs(offset) <= offset_max
    return ok, None if ok else "lipsync", conf, {"scorer": get_scorer().name, "offset_frames": offset}
