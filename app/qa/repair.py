"""Repair planning (guide 11.5): one action per failed shot.

 failure                                                         action                            resets shot to
 lipsync on a B2/C shot, sync_attempts < 2                       redo the lip-sync only            rendered
 lipsync on a B1 shot, or sync_attempts = 2                      re-render the clip                keyframe_ready
 face_match, or vision with category face or outfit              regenerate the keyframe, then clip planned
 vision with category action, acting or artefact                 re-render the clip                keyframe_ready
 clipping                                                        rebuild the shot audio at -3 dB   ready (recompile, no vendor call)
 attempts = 2 on the main model                                  switch to the fallback route      keyframe_ready (fallback_used)
 attempts = 3                                                    no more automatic work            needs_human
A shot is failed when any check failed, other than category 'unscored'. Several failures: the earliest stage wins.
"""
from __future__ import annotations

from app import db
from app.media import durations
from app.media.routing import method_tier, needs_lipsync

MAX_ATTEMPTS = 3
GAIN_STEP_DB = -3.0
MIN_GAIN_DB = -9.0
STAGE = {"keyframe": 0, "rerender": 1, "rebuild": 1, "lipsync": 2, "audio": 3}


def failures(shot_id) -> list[tuple[str, str | None]]:
    """(check, category) of each failed, scored check of the shot's latest QA round and latest render attempt."""
    rows = db.fetch_all("select check_name, category, passed, qa_round, detail from qa_results where shot_id = %s and qa_round in ('qa1','qa2')", (shot_id,))
    if not rows:
        return []
    latest_round = max(r["qa_round"] for r in rows)
    rows = [r for r in rows if r["qa_round"] == latest_round]
    newest = max((int(r["detail"].get("attempts", 0)), int(r["detail"].get("sync_attempts", 0))) for r in rows)
    return [(r["check_name"], r["category"]) for r in rows
            if (int(r["detail"].get("attempts", 0)), int(r["detail"].get("sync_attempts", 0))) == newest and not r["passed"] and r["category"] != "unscored"]


def wanted(shot: dict, fails: list[tuple[str, str | None]]) -> str | None:
    acts = set()
    for check, category in fails:
        if check == "face_match" or (check == "vision" and category in ("face", "outfit")):
            acts.add("keyframe")
        elif check == "vision":
            acts.add("rerender")
        elif check == "lipsync":
            can_sync = needs_lipsync(shot["tier"], shot["line_count"], shot["fallback_used"]) and shot["sync_attempts"] < 2
            acts.add("lipsync" if can_sync else "rerender")
        elif check == "clipping":
            acts.add("audio")
        elif check == "asset_intact":
            acts.add("rebuild")      # a brand insert that is not the supplied screen is rebuilt, never shipped
    return min(acts, key=STAGE.get) if acts else None


def decide(shot: dict, fails: list[tuple[str, str | None]], max_attempts: int = MAX_ATTEMPTS) -> dict | None:
    """{'action', 'sql' (assignments), 'params'} for one failed shot, or None when no scored check failed."""
    act = wanted(shot, fails)
    if act is None:
        return None
    if act == "lipsync":
        return {"action": "lipsync", "set": {"status": "rendered", "sync_attempts": shot["sync_attempts"] + 1}, "reset": True}
    if act == "audio":
        gain = float(shot["spec"].get("audio_gain_db", 0)) + GAIN_STEP_DB
        if gain < MIN_GAIN_DB:
            return {"action": "needs_human", "set": {"status": "needs_human"}, "reset": False}
        return {"action": "audio", "set": {"status": "ready"}, "spec": {"audio_gain_db": gain}, "reset": True}
    if shot["attempts"] >= max_attempts:
        return {"action": "needs_human", "set": {"status": "needs_human"}, "reset": False}
    if act == "rebuild":     # no vendor model is involved, so there is no fallback route: build it again (the attempt number makes a new ledger key)
        return {"action": "rebuild", "set": {"status": "keyframe_ready", "attempts": shot["attempts"] + 1}, "reset": True}
    if shot["attempts"] == max_attempts - 1 and not shot["fallback_used"]:
        mt = method_tier(shot["tier"], True, shot["line_count"])
        planned = float(shot["planned_duration"])
        return {"action": "fallback", "reset": True,
                "set": {"status": "keyframe_ready", "attempts": shot["attempts"] + 1, "sync_attempts": 0, "fallback_used": True,
                        "render_duration": durations.render_seconds(mt, planned)}}
    if act == "keyframe":
        return {"action": "keyframe", "set": {"status": "planned", "attempts": shot["attempts"] + 1, "sync_attempts": 0, "keyframe_r2_key": None}, "reset": True}
    return {"action": "rerender", "set": {"status": "keyframe_ready", "attempts": shot["attempts"] + 1, "sync_attempts": 0}, "reset": True}


def apply(conn, shot: dict, plan: dict) -> None:
    from psycopg.types.json import Jsonb

    cols = list(plan["set"])
    conn.execute(f"update shots set {', '.join(f'{c} = %s' for c in cols)} where id = %s", (*[plan['set'][c] for c in cols], shot["id"]))
    if plan.get("spec"):
        conn.execute("update shots set spec = spec || %s where id = %s", (Jsonb(plan["spec"]), shot["id"]))
