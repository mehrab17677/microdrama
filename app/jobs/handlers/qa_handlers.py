"""QA and learning handlers (guide 11): qa_shot, qa_episode, llm_rule. Every check writes one qa_results row."""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

from psycopg.types.json import Jsonb

from app import db
from app.config import dry_run, get_config
from app.jobs.handlers import look_handlers
from app.media import ffmpeg
from app.media.routing import needs_lipsync
from app.qa import episode as episode_qa, lipsync, shot as shot_qa, vision
from app.stages import look
from app.vendors import storage

KINDS = ["qa_shot", "qa_episode", "llm_rule"]


def _vec(text: str | None) -> list[float] | None:
    return None if text is None else [float(x) for x in text.strip("[]").split(",")]


def _asset(character_id, kind: str, **tags):
    sql, params = "select r2_key_clean, face_vector::text fv from character_assets where character_id = %s and kind = %s", [character_id, kind]
    for k, v in tags.items():
        sql += f" and tags->>'{k}' = %s"
        params.append(v)
    return db.fetch_one(sql + " order by created_at limit 1", params)


def qa_shot(job: dict) -> dict:
    sid, rnd = job["series_id"], job["request"]["round"]
    shot = db.fetch_one(
        "select s.*, e.number episode_number, cardinality(s.line_ids) line_count from shots s join episodes e on e.id = s.episode_id where s.id = %s", (job["shot_id"],))
    spec, thresholds = shot["spec"], get_config("qa_thresholds")
    rows: list[dict] = []   # {check, passed, score, category, detail}

    def add(check, passed, score=None, category=None, detail=None):
        rows.append({"check": check, "passed": bool(passed), "score": score, "category": category, "detail": detail or {}})

    if shot["tier"] == "INSERT":
        add("clipping", True, detail={"insert": True})   # a static insert has no actors to match; there is nothing to judge
    else:
        speaker_code = next((c["code"] for c in spec["characters"] if c.get("mouth_visible_speaking")), None) or (spec["characters"][0]["code"] if spec["characters"] else None)
        char = db.fetch_one("select id from characters where series_id = %s and code = %s", (sid, speaker_code)) if speaker_code else None
        master = _asset(char["id"], "master") if char else None
        outfit_code = next((c["outfit_code"] for c in spec["characters"] if c["code"] == speaker_code), None)
        outfit = _asset(char["id"], "wardrobe", outfit=outfit_code) if char and outfit_code else None
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mp4"
            storage.get_to_file(shot["final_clip_r2_key"], clip)
            info = ffmpeg.probe(clip)
            # Dry-run media has no real faces and the fixture vision answer ignores the images, so the four ffmpeg frame grabs are skipped there.
            frames = vision.extract_frames(clip, info["duration"], tmp) if (not dry_run() or look_handlers._real_faces()) else []
            # face_match: dry-run media has no real faces, so there is nothing to compare
            if master is None or master["fv"] is None:
                add("face_match", True, detail={"no_master": True})
            elif not look_handlers._real_faces():
                add("face_match", True, detail={"dry_run": True})
            else:
                ok, score, detail = shot_qa.face_match(frames, _vec(master["fv"]), thresholds["face_match_min"])
                add("face_match", ok, score, None if ok else "face", detail)
            # lip-sync (speaking shots only)
            if shot["line_count"]:
                ok, cat, score, detail = lipsync.judge(lipsync.get_scorer().score(str(clip)), thresholds["lipsync_conf_min"], thresholds["lipsync_offset_max"])
                add("lipsync", ok, score, cat, detail)
            # vision
            emotion = None
            if shot["line_ids"]:
                emotion = db.fetch_one("select emotion from dialogue_lines where id = %s", (shot["line_ids"][0],))["emotion"]
            role = "vision_qa_strong" if shot["shot_type"] == "hero" else "vision_qa"
            v, _usage = vision.check(role, frames, master and look.fetch_bytes(master["r2_key_clean"]), outfit and look.fetch_bytes(outfit["r2_key_clean"]), spec["action"], emotion)
            add("vision", vision.passed(v), None, None if vision.passed(v) else (v.category or "artefact"), v.model_dump())
            # clipping: the shot audio is what is heard
            audio = clip
            if shot["line_ids"]:
                audio = Path(tmp) / "shot_audio.mp3"
                try:
                    storage.get_to_file(f"series/{sid}/episodes/{shot['episode_number']:02d}/shots/{shot['idx']}/shot_audio.mp3", audio)
                except Exception:   # noqa: BLE001 - not built (should not happen for a ready shot); judge the clip itself
                    audio = clip
            ok, level, detail = shot_qa.clipping(audio, float(spec.get("audio_gain_db", 0)), thresholds["clipping_max_db"])
            add("clipping", ok, level, None if ok else "clipping", detail)

    failed = [r for r in rows if not r["passed"] and r["category"] != "unscored"]
    # Rows are kept per render attempt, so a failure that was repaired stays on record (the learn step writes rules from it).
    # Checking the same attempt again (after a clipping repair) replaces that attempt's rows.
    attempt = {"attempts": shot["attempts"], "sync_attempts": shot["sync_attempts"]}
    with db.transaction() as conn:
        conn.execute("delete from qa_results where shot_id = %s and qa_round = %s and (detail->>'attempts')::int = %s and (detail->>'sync_attempts')::int = %s",
                     (shot["id"], rnd, shot["attempts"], shot["sync_attempts"]))
        for r in rows:
            conn.execute("insert into qa_results (series_id, episode_id, shot_id, qa_round, check_name, passed, score, category, detail) values (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                         (sid, shot["episode_id"], shot["id"], rnd, r["check"], r["passed"], r["score"], r["category"], Jsonb({**r["detail"], **attempt})))
        conn.execute("update shots set status = %s where id = %s", ("qa_failed" if failed else "qa_passed", shot["id"]))
    return {"failed": [r["check"] for r in failed], "unscored": [r["check"] for r in rows if r["category"] == "unscored"]}


def qa_episode(job: dict) -> dict:
    sid, rnd = job["series_id"], job["request"]["round"]
    ep = db.fetch_one("select * from episodes where id = %s", (job["episode_id"],))
    if not ep["final_r2_key"]:
        from app.jobs.errors import PermanentError

        raise PermanentError("the episode has not been compiled")
    th = get_config("qa_thresholds")
    shots = db.fetch_all("select s.id, s.idx, s.spec, s.planned_duration, s.line_ids from shots s where s.episode_id = %s order by s.idx", (ep["id"],))
    rows: list[tuple] = []   # (check, passed, score, detail)
    ok, total, detail = episode_qa.runtime_check([float(s["planned_duration"]) for s in shots])
    rows.append(("runtime", ok, total, detail))
    rows.append(("hook", *episode_qa.hook_check(shots[0]["spec"], float(shots[0]["planned_duration"]), th["hook_max_seconds"])))
    rows.append(("cliffhanger", *episode_qa.cliffhanger_check(shots[-1]["spec"])))
    texts = [r["text"] for r in db.fetch_all("select text from dialogue_lines where episode_id = %s order by idx", (ep["id"],))]
    usage = None
    with tempfile.TemporaryDirectory() as tmp:
        final = Path(tmp) / "final.mp4"
        storage.get_to_file(ep["final_r2_key"], final)
        rows.append(("loudness", *episode_qa.loudness_check(final, th["loudness_target"], th["loudness_tolerance"])))
        sheet = None
        if not dry_run() or look_handlers._real_faces():   # the fixture answer ignores the image
            marks = db.fetch_all("select shot_id, start_seconds, end_seconds from edit_entries where episode_id = %s and cut_version = %s", (ep["id"], ep["cut_version"]))
            at = {m["shot_id"]: (float(m["start_seconds"]) + float(m["end_seconds"])) / 2 for m in marks}
            frames = []
            for s in shots:
                path = Path(tmp) / f"c{s['idx']}.png"
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{at.get(s['id'], 0):.3f}", "-i", str(final), "-frames:v", "1", str(path)], check=True)
                frames.append((s["idx"], path))
            sheet = episode_qa.contact_sheet(frames)
        ok, issues, usage = episode_qa.continuity_check(sheet)
        rows.append(("continuity", ok, None, {"issues": issues}))
    ass = _read_text(ep["subtitle_r2_key"])
    rows.append(("captions", *episode_qa.captions_check(ass, texts)))
    unscored = [r["idx"] for r in db.fetch_all(
        "select s.idx from qa_results q join shots s on s.id = q.shot_id where s.episode_id = %s and q.category = 'unscored' order by s.idx", (ep["id"],))]
    rows.append(("lipsync_review", True, None, {"scorer": lipsync.get_scorer().name, "unscored_shots": sorted(set(unscored)),
                                                "note": "No lip-sync scorer is configured: lip-sync is judged by people at the gates." if lipsync.get_scorer().name == "none" else "scored"}))
    with db.transaction() as conn:
        conn.execute("delete from qa_results where episode_id = %s and shot_id is null and qa_round = %s", (ep["id"], rnd))
        for check, passed, score, detail in rows:
            conn.execute("insert into qa_results (series_id, episode_id, qa_round, check_name, passed, score, detail) values (%s,%s,%s,%s,%s,%s,%s)",
                         (sid, ep["id"], rnd, check, bool(passed), score, Jsonb(detail)))
    return {"failed": [c for c, p, _, _ in rows if not p], "actual_cost": round(usage["cost_usd"], 6) if isinstance(usage, dict) and "cost_usd" in usage else 0.0}


def _read_text(key: str) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "f.ass"
        storage.get_to_file(key, path)
        return path.read_text(encoding="utf-8")



def llm_rule(job: dict) -> dict:
    from app.jobs.errors import PermanentError
    from app.media.routing import model_key
    from app.stages import learning

    shot = db.fetch_one("select s.*, cardinality(s.line_ids) line_count from shots s where s.id = %s", (job["shot_id"],))
    check = job["request"]["check"]
    out, usage = learning.write_rule(shot, check)
    text = out.rule.strip()
    if not text or len(text.split()) > 25:
        raise PermanentError(f"a rule is one sentence of at most 25 words, not {len(text.split())}")
    category = (db.fetch_one("select category from qa_results where shot_id = %s and check_name = %s and passed = false order by qa_round desc limit 1", (shot["id"], check)) or {}).get("category") or check
    rule_id, inserted, embed_usage = learning.store_rule(model_key(shot["tier"], shot["fallback_used"], shot["line_count"]), text, category, shot["id"])
    from app.stages.ingest import _cost_rows

    usages = [usage, embed_usage]
    return {"rule_id": rule_id, "inserted": inserted, "actual_cost": round(sum(u["cost_usd"] for u in usages), 6), "cost_log": _cost_rows(usages)}


HANDLERS = {kind: SimpleNamespace(run=globals()[kind]) for kind in KINDS}
