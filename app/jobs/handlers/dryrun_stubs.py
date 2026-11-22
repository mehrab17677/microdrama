"""DRY_RUN stand-ins for every job kind that has no real handler yet (guide 7.7, phase 5).

Each stub writes the minimal rows and fixture files the next graph step expects, so a whole
production can run from start to done at no cost. They are registered ONLY when DRY_RUN=1 (see
load_stubs), so a real run can never silently fake work. Phases 6 to 12 replace them kind by kind.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

from psycopg.types.json import Jsonb

from app import db
from app.config import dry_run, get_config
from app.graphs import common, work
from app.media import ffmpeg, requests as media_requests
from app.media.routing import needs_lipsync, tier_for
from app.qa import face
from app.stages import look
from app.vendors import eleven, falmedia, fixtures, storage

LINES_PER_EPISODE = 15      # five speaking shots of three lines each
SPEAKING_SHOTS = 5
EMOTIONS = ["neutral", "angry", "sad"]
EMOTION_GROUPS = {"neutral": "calm", "angry": "anger", "sad": "sadness"}
VISUAL = ("A woman in her late twenties with shoulder-length dark hair, warm brown eyes and a calm, watchful expression, "
          "wearing a plain dark jacket over a cream blouse, soft natural makeup, an unadorned silver chain, composed posture, "
          "quiet confidence, understated styling, even skin tone and a faint scar through the left eyebrow.")


def _tmp_file(data: bytes, suffix: str) -> Path:
    f = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    f.write(data)
    f.close()
    return Path(f.name)


def _put(key: str, fixture: str) -> None:
    storage.put_bytes(key, fixtures.fixture_bytes(fixture))


def _chars(sid) -> list[dict]:
    return db.fetch_all("select * from characters where series_id = %s order by code", (sid,))


def _ep(job) -> dict:
    return db.fetch_one("select * from episodes where id = %s", (job["episode_id"],))


# ------------------------------------------------------------------ story

def llm_bible(job):
    sid = job["series_id"]
    roles = [r["role_label"] for r in db.fetch_all("select role_label from brief_refs where series_id = %s and kind = 'character' order by created_at", (sid,))] or [None]
    chars = [{"code": f"CH{i:02d}", "slot_role": role, "name": f"Lead {i}", "role": role or "lead", "is_lead": True, "age": 28, "personality": "guarded",
              "visual_description": VISUAL, "speaks": True} for i, role in enumerate(roles, 1)]
    chars.append({"code": f"CH{len(chars) + 1:02d}", "slot_role": None, "name": "Supporting", "role": "rival", "is_lead": False, "age": 35, "personality": "proud",
                  "visual_description": VISUAL, "speaks": True})
    sets = [{"code": f"SET{i:02d}", "name": f"Place {i}", "description": "A lived-in interior with soft practical light.", "times_of_day": ["day", "night"] if i <= 2 else ["day"]} for i in range(1, 5)]
    n = int(get_config("dryrun_episode_count"))
    content = {"title": "The Quiet Debt", "logline": "A woman returns to settle an old debt.", "tone": "tense", "central_conflict": "debt versus trust",
               "world_rules": [], "characters": chars, "sets": sets, "episode_count": n, "series_summary": "A fixture story bible."}
    with db.transaction() as conn:
        for table in ("episodes", "characters", "sets"):
            conn.execute(f"delete from {table} where series_id = %s", (sid,))
        version = conn.execute("select coalesce(max(version), 0) + 1 v from story_bibles where series_id = %s", (sid,)).fetchone()["v"]
        conn.execute("insert into story_bibles (series_id, version, content, series_summary) values (%s, %s, %s, %s)", (sid, version, Jsonb(content), content["series_summary"]))
        for c in chars:
            conn.execute("insert into characters (series_id, code, name, role, is_lead, visual_description) values (%s, %s, %s, %s, %s, %s)",
                         (sid, c["code"], c["name"], c["role"], c["is_lead"], c["visual_description"]))
        for s in sets:
            for tod in s["times_of_day"]:
                conn.execute("insert into sets (series_id, code, name, description, time_of_day) values (%s, %s, %s, %s, %s)", (sid, s["code"], s["name"], s["description"], tod))
        common.set_series(sid, conn=conn, title=content["title"], episode_count=n)
    return {"episode_count": n}


def llm_beats(job):
    sid = job["series_id"]
    n = int(common.get_series(sid)["episode_count"])
    codes = [c["code"] for c in _chars(sid)]
    with db.transaction() as conn:
        conn.execute("delete from episodes where series_id = %s", (sid,))
        for i in range(1, n + 1):
            beat = {"number": i, "summary": f"Episode {i} moves the debt plot forward.", "character_codes": codes, "set_codes": ["SET01"],
                    "hook": "A letter arrives.", "cliffhanger": "The door opens.", "handoff": {c: "knows the secret" for c in codes}, "placement": None}
            conn.execute("insert into episodes (series_id, number, batch, status, beat) values (%s, %s, %s, 'outlined', %s)", (sid, i, "pilot" if i <= 3 else "full", Jsonb(beat)))
    return {"episodes": n}


def llm_placement_plan(job):
    sid = job["series_id"]
    asset = db.fetch_one("select id from brand_assets where series_id = %s order by created_at limit 1", (sid,))
    with db.transaction() as conn:
        conn.execute("delete from placements where series_id = %s", (sid,))
        for number in (2, 5):
            ep = conn.execute("select id, beat from episodes where series_id = %s and number = %s", (sid, number)).fetchone()
            if not ep:
                continue
            beat = {**ep["beat"], "placement": {"kind": "product_in_use", "description": "The lead checks the brand app."}}
            conn.execute("update episodes set beat = %s where id = %s", (Jsonb(beat), ep["id"]))
            conn.execute("insert into placements (series_id, episode_number, kind, description, brand_asset_id) values (%s, %s, 'product_in_use', 'The lead checks the brand app.', %s)",
                         (sid, number, asset and asset["id"]))
    return {}


def llm_script(job):
    sid = job["series_id"]
    ep = _ep(job)
    chars = _chars(sid)
    with db.transaction() as conn:
        conn.execute("delete from dialogue_lines where episode_id = %s", (ep["id"],))
        for i in range(LINES_PER_EPISODE):
            speaker = chars[(i // 3) % len(chars)]  # three consecutive lines per speaker, so a shot has one speaker
            conn.execute("insert into dialogue_lines (series_id, episode_id, idx, character_id, text, emotion) values (%s, %s, %s, %s, %s, %s)",
                         (sid, ep["id"], i, speaker["id"], f"Line {i} of episode {ep['number']}.", EMOTIONS[i % 3]))
        script = {"number": ep["number"], "hook_action": "A letter arrives.", "scenes": [{"index": 1, "set_code": "SET01", "time_of_day": "day", "action": "Two people argue.", "lines": []}],
                  "cliffhanger_action": "The door opens.", "pronunciations": []}
        conn.execute("update episodes set script = %s, status = 'scripted' where id = %s", (Jsonb(script), ep["id"]))
    return {}


def llm_continuity(job):
    return {"edits": 0}


def llm_wardrobe(job):
    sid = job["series_id"]
    with db.transaction() as conn:
        conn.execute("delete from wardrobe_items where series_id = %s", (sid,))
        for c in _chars(sid):
            conn.execute("insert into wardrobe_items (series_id, character_id, code, description, scenes) values (%s, %s, 'W01', 'Dark jacket over a cream blouse.', %s)", (sid, c["id"], Jsonb([{"episode": 1, "scene": 1}])))
    return {}


def llm_originality(job):
    sid = job["series_id"]
    with db.transaction() as conn:
        conn.execute("delete from qa_results where series_id = %s and check_name = 'originality'", (sid,))
        conn.execute("insert into qa_results (series_id, qa_round, check_name, passed, detail) values (%s, 'precheck', 'originality', true, %s)", (sid, Jsonb({"same_story": False})))
    return {"same_story": False}


# ------------------------------------------------------------------ look

def llm_trait_sheet(job):
    return {"trait_sheet": "Adult, medium build, dark hair, calm and watchful."}


def llm_voice_profile(job):
    series = common.get_series(job["series_id"])
    profile = {"voice_gender": "female", "voice_age": "adult", "pitch": "low", "pace": "measured", "accent": "Bangladeshi" if series["language"] == "bn" else "neutral",
               "texture": "warm", "energy": "restrained", "speech_habits": "pauses before hard truths", "design_prompt": look.example_design_prompt(series["language"])}
    db.execute("update characters set voice_profile = %s where id = %s", (Jsonb(profile), job["request"]["character_id"]))
    return {}


def llm_audio_tag(job):
    req = job["request"]
    if req["purpose"] == "emotion_groups":
        emotions = [r["emotion"] for r in db.fetch_all("select distinct emotion from dialogue_lines where character_id = %s", (req["character_id"],))]
        mapping = {e: EMOTION_GROUPS.get(e, "calm") for e in emotions} or {"neutral": "calm"}
        db.execute("update characters set emotion_groups = %s where id = %s", (Jsonb(mapping), req["character_id"]))
    else:
        db.execute("update dialogue_lines set audio_tag = lower(emotion) where episode_id = %s and audio_tag is null", (job["episode_id"],))
    return {}


def image_master(job):
    sid, req = job["series_id"], job["request"]
    char = db.fetch_one("select * from characters where id = %s", (req["character_id"],))
    asset_id = _new_id()
    base = f"series/{sid}/characters/{char['code']}/{asset_id}"
    _put(base + "_clean.png", "image_1080x1920.png")
    _put(base + "_review.png", "image_1080x1920.png")
    db.execute(
        "insert into character_assets (id, series_id, character_id, kind, tags, r2_key_review, r2_key_clean, face_vector, version) values (%s, %s, %s, 'master', '{}'::jsonb, %s, %s, %s::vector, %s)",
        (asset_id, sid, char["id"], base + "_review.png", base + "_clean.png", db.to_vector(face.stub_vector(f"{char['code']}:{req['try']}")), req["try"]),
    )
    return {"asset_id": str(asset_id)}


def image_pack(job):
    sid, req = job["series_id"], job["request"]
    char = req["character_id"] and db.fetch_one("select * from characters where id = %s", (req["character_id"],))
    asset_id = _new_id()
    folder = char["code"] if char else "lineup"
    base = f"series/{sid}/characters/{folder}/{asset_id}"
    _put(base + "_clean.png", "image_1080x1920.png")
    _put(base + "_review.png", "image_1080x1920.png")
    vector = None
    if char:
        m = db.fetch_one("select face_vector::text fv from character_assets where character_id = %s and kind = 'master' limit 1", (char["id"],))
        vector = m and m["fv"]
    db.execute(
        "insert into character_assets (id, series_id, character_id, kind, tags, r2_key_review, r2_key_clean, face_vector) values (%s, %s, %s, %s, %s, %s, %s, %s::vector)",
        (asset_id, sid, char and char["id"], req["kind"], Jsonb(req["tags"]), base + "_review.png", base + "_clean.png", vector),
    )
    return {"asset_id": str(asset_id)}


def image_set_plate(job):
    sid = job["series_id"]
    s = db.fetch_one("select * from sets where id = %s", (job["request"]["set_id"],))
    key = f"series/{sid}/sets/{s['code']}_{s['time_of_day']}.png"
    _put(key, "image_1080x1920.png")
    db.execute("update sets set plate_r2_key = %s where id = %s", (key, s["id"]))
    return {}


def sfx_ambience(job):
    sid, code = job["series_id"], job["request"]["set_code"]
    audio, usage = eleven.sound_effect(f"ambience of {code}", 30, loop=True)
    key = f"series/{sid}/sets/{code}_ambience.mp3"
    storage.put_bytes(key, audio)
    db.execute("update sets set ambience_r2_key = %s where series_id = %s and code = %s", (key, sid, code))
    return {"actual_cost": usage["cost_usd"]}


def voice_design(job):
    sid = job["series_id"]
    char = db.fetch_one("select * from characters where id = %s", (job["request"]["character_id"],))
    previews, _ = eleven.design_voice((char["voice_profile"] or {}).get("design_prompt", "A calm adult voice."))
    voice_id = eleven.save_voice(previews[0]["generated_voice_id"], char["code"], "stub voice")
    asset_id = _new_id()
    key = f"series/{sid}/characters/{char['code']}/{asset_id}_voice.mp3"
    storage.put_bytes(key, previews[0]["audio"])
    db.execute("insert into character_assets (id, series_id, character_id, kind, tags, r2_key_clean) values (%s, %s, %s, 'voice_sample', %s, %s)",
               (asset_id, sid, char["id"], Jsonb({"voice_id": voice_id}), key))
    return {"voice_id": voice_id, "asset_id": str(asset_id)}


def _new_id():
    import uuid

    return uuid.uuid4()


# ------------------------------------------------------------------ build

def llm_shot_list(job):
    """Ten shots: a non-speaking shot before each of five speaking shots (three lines each), alternating
    close-up (tier B1) and wider (tier B2). The first shot is the hook, the last the cliffhanger."""
    sid = job["series_id"]
    ep = _ep(job)
    lines = db.fetch_all("select id, idx, character_id from dialogue_lines where episode_id = %s order by idx", (ep["id"],))
    chars = {c["id"]: c for c in _chars(sid)}
    first_char = _chars(sid)[0]
    setrow = db.fetch_one("select id from sets where series_id = %s and code = 'SET01' and time_of_day = 'day'", (sid,))
    size = -(-len(lines) // SPEAKING_SHOTS)
    chunks = [lines[i:i + size] for i in range(0, len(lines), size)][:SPEAKING_SHOTS]
    plan: list[list[dict] | None] = []
    for chunk in chunks:
        plan += [None, chunk]
    with db.transaction() as conn:
        conn.execute("delete from shots where episode_id = %s", (ep["id"],))
        for idx, chunk in enumerate(plan):
            speaking = chunk is not None
            speaker = chars[chunk[0]["character_id"]] if speaking else first_char
            stype = ("speaking_closeup" if (idx // 2) % 2 == 0 else "speaking_wider") if speaking else "non_speaking"
            spec = {"idx": idx, "shot_type": stype, "scene_index": 1, "set_code": "SET01", "time_of_day": "day", "camera_setup_name": "Static medium", "new_setup": None,
                    "characters": [{"code": speaker["code"], "outfit_code": "W01", "position": "frame center", "mouth_visible_speaking": speaking}],
                    "line_indexes": [l["idx"] for l in chunk or []], "action": "A quiet exchange." if speaking else "A held look.", "sounds": ["room tone"],
                    "is_hook": idx == 0, "is_cliffhanger": idx == len(plan) - 1, "placement_kind": None, "est_seconds": 3.5 if idx == 0 else 9.0, "emotion_group": "calm"}
            conn.execute(
                "insert into shots (series_id, episode_id, idx, shot_type, tier, set_id, spec, line_ids, speaker_character_id, status)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'planned')",
                (sid, ep["id"], idx, stype, tier_for(stype), setrow["id"], Jsonb(spec), [l["id"] for l in chunk or []], speaker["id"] if speaking else None),
            )
        conn.execute("update episodes set status = 'shots_planned' where id = %s", (ep["id"],))
    return {}


def tts_line(job):
    sid = job["series_id"]
    line = db.fetch_one("select l.*, c.voice_id from dialogue_lines l join characters c on c.id = l.character_id where l.id = %s", (job["request"]["line_id"],))
    ep = _ep(job)
    pairs = [(p["term"], p["spoken_as"]) for p in db.fetch_all("select term, spoken_as from pronunciations where series_id = %s", (sid,))]
    text = eleven.apply_pronunciations(line["text"], pairs)
    if line["audio_tag"]:
        text = f"[{line['audio_tag']}] {text}"
    language = common.get_series(sid)["language"]
    out, usage = eleven.speech(text, line["voice_id"] or "dry-voice", language, eleven.stable_seed(str(line["id"]), line["version"]))
    key = f"series/{sid}/episodes/{ep['number']:02d}/audio/line_{line['idx']}.mp3"
    storage.put_bytes(key, out["audio"])
    path = _tmp_file(out["audio"], ".mp3")
    try:
        seconds = ffmpeg.duration(path)  # measured from the saved file, not taken from the alignment
    finally:
        path.unlink(missing_ok=True)
    db.execute("update dialogue_lines set audio_r2_key = %s, duration_seconds = %s, alignment = %s where id = %s", (key, seconds, Jsonb({"words": out["words"]}), line["id"]))
    return {"duration": seconds, "actual_cost": usage["cost_usd"]}


def music_episode(job):
    sid, ep = job["series_id"], _ep(job)
    audio, usage = eleven.music("Tense instrumental underscore, no vocals", int(job["request"]["length_ms"]))
    key = f"series/{sid}/episodes/{ep['number']:02d}/music.mp3"
    storage.put_bytes(key, audio)
    db.execute("update episodes set music_r2_key = %s where id = %s", (key, ep["id"]))
    return {"actual_cost": usage["cost_usd"]}


def sfx_effect(job):
    return {}


def llm_shot_request(job):
    shot = db.fetch_one("select spec from shots where id = %s", (job["shot_id"],))
    return {"prompt": f"{shot['spec']['action']} Soft light, slow camera.", "camera_fixed": False}


def image_keyframe(job):
    sid = job["series_id"]
    shot = db.fetch_one("select s.*, e.number episode_number from shots s join episodes e on e.id = s.episode_id where s.id = %s", (job["shot_id"],))
    key = f"series/{sid}/episodes/{shot['episode_number']:02d}/shots/{shot['idx']}/keyframe.png"
    _put(key, "image_1080x1920.png")
    cache_key = work.keyframe_cache_key(shot["spec"])
    with db.transaction() as conn:
        conn.execute("update shots set keyframe_r2_key = %s, status = 'keyframe_ready' where id = %s", (key, shot["id"]))
        if shot["attempts"] == 0:  # shots sharing this cache key take the same keyframe
            for other in conn.execute("select id, spec from shots where series_id = %s and status = 'planned' and attempts = 0 and tier <> 'INSERT'", (sid,)).fetchall():
                if work.keyframe_cache_key(other["spec"]) == cache_key:
                    conn.execute("update shots set keyframe_r2_key = %s, status = 'keyframe_ready' where id = %s", (key, other["id"]))
    return {}


def _shot_row(job) -> dict:
    return db.fetch_one("select s.*, e.number episode_number, cardinality(s.line_ids) line_count from shots s join episodes e on e.id = s.episode_id where s.id = %s", (job["shot_id"],))


class _VideoClip:
    @staticmethod
    def submit(job):
        req = job["request"]
        if req.get("shot_audio_key"):
            _put(req["shot_audio_key"], "speech_2s.mp3")  # stands in for build_shot_audio (phase 8)
        return falmedia.submit(job["endpoint"], media_requests.resolve_keys(req["arguments"]))

    @staticmethod
    def finalize(job):
        shot = _shot_row(job)
        attempt = job["request"]["attempt"]
        local = falmedia.download(falmedia.video_url(job["result"]), Path(tempfile.mkdtemp()) / "clip.mp4")
        info = ffmpeg.probe(local)
        key = f"series/{job['series_id']}/episodes/{shot['episode_number']:02d}/shots/{shot['idx']}/raw_a{attempt}.mp4"
        storage.put_file(key, local)
        with db.transaction() as conn:
            if (info["width"], info["height"]) != (1080, 1920):
                conn.execute("insert into qa_results (series_id, episode_id, shot_id, qa_round, check_name, passed, detail) values (%s, %s, %s, 'precheck', 'resolution', false, %s)",
                             (job["series_id"], shot["episode_id"], shot["id"], Jsonb(info)))
            if needs_lipsync(shot["tier"], shot["line_count"]):
                conn.execute("update shots set raw_clip_r2_key = %s, status = 'rendered' where id = %s", (key, shot["id"]))
            else:
                conn.execute("update shots set raw_clip_r2_key = %s, final_clip_r2_key = %s, status = 'ready' where id = %s", (key, key, shot["id"]))
        return {"probe": info}


class _Lipsync:
    @staticmethod
    def submit(job):
        return falmedia.submit(job["endpoint"], media_requests.resolve_keys(job["request"]["arguments"]))

    @staticmethod
    def finalize(job):
        shot = _shot_row(job)
        local = falmedia.download(falmedia.video_url(job["result"]), Path(tempfile.mkdtemp()) / "synced.mp4")
        info = ffmpeg.probe(local)
        key = f"series/{job['series_id']}/episodes/{shot['episode_number']:02d}/shots/{shot['idx']}/synced_a{job['request']['sync_attempt']}.mp4"
        storage.put_file(key, local)
        db.execute("update shots set final_clip_r2_key = %s, status = 'ready' where id = %s", (key, shot["id"]))
        return {"probe": info}


def ffmpeg_insert(job):
    shot = _shot_row(job)
    key = f"series/{job['series_id']}/episodes/{shot['episode_number']:02d}/shots/{shot['idx']}/insert.mp4"
    _put(key, "clip_4s_1080x1920.mp4")
    db.execute("update shots set final_clip_r2_key = %s, status = 'ready' where id = %s", (key, shot["id"]))
    return {}


def ffmpeg_compile(job):
    sid, ep = job["series_id"], _ep(job)
    cut = job["request"]["cut"]
    base = f"series/{sid}/episodes/{ep['number']:02d}"
    _put(f"{base}/final_v{cut}.mp4", "clip_4s_1080x1920.mp4")
    storage.put_bytes(f"{base}/final_v{cut}.ass", b"[Script Info]\n")
    shots = db.fetch_all("select id, planned_duration from shots where episode_id = %s order by idx", (ep["id"],))
    t = 0.0
    with db.transaction() as conn:
        conn.execute("delete from edit_entries where episode_id = %s and cut_version = %s", (ep["id"], cut))
        for s in shots:
            end = t + float(s["planned_duration"])
            conn.execute("insert into edit_entries (episode_id, cut_version, shot_id, start_seconds, end_seconds) values (%s, %s, %s, %s, %s)", (ep["id"], cut, s["id"], t, end))
            t = end
        conn.execute("update episodes set final_r2_key = %s, subtitle_r2_key = %s, cut_version = %s, runtime_seconds = %s, status = 'compiled' where id = %s",
                     (f"{base}/final_v{cut}.mp4", f"{base}/final_v{cut}.ass", cut, t, ep["id"]))
    return {"runtime": t}


def qa_shot(job):
    sid, rnd = job["series_id"], job["request"]["round"]
    shot = _shot_row(job)
    with db.transaction() as conn:
        conn.execute("delete from qa_results where shot_id = %s and qa_round = %s", (shot["id"], rnd))
        for name in ("face_match", "vision", "clipping"):
            conn.execute("insert into qa_results (series_id, episode_id, shot_id, qa_round, check_name, passed) values (%s, %s, %s, %s, %s, true)", (sid, shot["episode_id"], shot["id"], rnd, name))
        if shot["line_count"]:  # no lip-sync scorer yet: recorded as unscored, judged by people at the gates
            conn.execute("insert into qa_results (series_id, episode_id, shot_id, qa_round, check_name, passed, category) values (%s, %s, %s, %s, 'lipsync', false, 'unscored')", (sid, shot["episode_id"], shot["id"], rnd))
        conn.execute("update shots set status = 'qa_passed' where id = %s", (shot["id"],))
    return {}


def qa_episode(job):
    sid, rnd, ep = job["series_id"], job["request"]["round"], _ep(job)
    with db.transaction() as conn:
        conn.execute("delete from qa_results where episode_id = %s and shot_id is null and qa_round = %s", (ep["id"], rnd))
        for name in ("runtime", "hook", "cliffhanger", "loudness", "continuity", "captions"):
            conn.execute("insert into qa_results (series_id, episode_id, qa_round, check_name, passed) values (%s, %s, %s, %s, true)", (sid, ep["id"], rnd, name))
        conn.execute("update episodes set status = 'qa_passed' where id = %s", (ep["id"],))
    return {}


def qa_brand(job):
    sid, rnd, ep = job["series_id"], job["request"]["round"], _ep(job)
    with db.transaction() as conn:
        for name in ("placement_count", "asset_intact", "exclusions"):
            conn.execute("insert into qa_results (series_id, episode_id, qa_round, check_name, passed) values (%s, %s, %s, %s, true)", (sid, ep["id"], rnd, name))
        conn.execute("update placements set status = 'verified' where series_id = %s and episode_number = %s", (sid, ep["number"]))
    return {}


def llm_rule(job):
    return {"rule": None}


def ffmpeg_master(job):
    _put(f"series/{job['series_id']}/delivery/master.mp4", "clip_4s_1080x1920.mp4")
    return {}


def llm_marketing(job):
    return {"title": f"Episode {job['request']['episode']}", "teaser": "Fixture teaser.", "hooks": ["One.", "Two."]}


def llm_feedback_classify(job):
    return {"category": "picture"}


_FUNCS = {
    "llm_bible": llm_bible, "llm_beats": llm_beats, "llm_placement_plan": llm_placement_plan, "llm_script": llm_script, "llm_continuity": llm_continuity,
    "llm_wardrobe": llm_wardrobe, "llm_originality": llm_originality, "llm_trait_sheet": llm_trait_sheet, "llm_voice_profile": llm_voice_profile,
    "llm_audio_tag": llm_audio_tag, "image_master": image_master, "image_pack": image_pack, "image_set_plate": image_set_plate, "voice_design": voice_design,
    "sfx_ambience": sfx_ambience, "tts_line": tts_line, "music_episode": music_episode, "sfx_effect": sfx_effect, "llm_shot_list": llm_shot_list,
    "llm_shot_request": llm_shot_request, "image_keyframe": image_keyframe, "ffmpeg_insert": ffmpeg_insert, "ffmpeg_compile": ffmpeg_compile,
    "qa_shot": qa_shot, "qa_episode": qa_episode, "qa_brand": qa_brand, "llm_rule": llm_rule, "ffmpeg_master": ffmpeg_master, "llm_marketing": llm_marketing,
    "llm_feedback_classify": llm_feedback_classify,
}
HANDLERS = {kind: SimpleNamespace(run=fn) for kind, fn in _FUNCS.items()}
HANDLERS["video_clip"] = _VideoClip
HANDLERS["lipsync"] = _Lipsync
