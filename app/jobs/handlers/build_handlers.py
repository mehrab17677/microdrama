"""Real handlers for the build stages (guide 9.6): shot list, voice-over, keyframes, shot requests, music,
clips and lip-sync, set inserts and the compile. Roles: director (shot list), compiler (shot request).
Nothing is stored until the checks pass; a failed check raises PermanentError listing every problem."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

from psycopg.types.json import Jsonb

from app import db
from app.config import dry_run, get_config
from app.graphs import common, work
from app.jobs.errors import PermanentError
from app.media import compile as media_compile, ffmpeg, requests as media_requests, subtitles
from app.media.routing import MODEL_KEY_BY_TIER, needs_lipsync
from app.models.shots import ShotList, ShotRequest
from app.qa import face
from app.stages import look, shots as shots_stage, story
from app.stages.ingest import _cost_rows
from app.vendors import eleven, falmedia, images, llm, storage

KINDS = ["llm_shot_list", "tts_line", "music_episode", "llm_shot_request", "image_keyframe", "ffmpeg_insert", "ffmpeg_compile"]


def _result(usages: list[dict], **extra) -> dict:
    usages = [u for u in usages if u]
    return {**extra, "actual_cost": round(sum(u["cost_usd"] for u in usages), 6), "cost_log": _cost_rows(usages)}


def _fail_if(problems: list[str]) -> None:
    if problems:
        raise PermanentError(f"{len(problems)} problem(s): " + "; ".join(problems[:12]))


# ---------------------------------------------------------------- shot list

def llm_shot_list(job: dict) -> dict:
    sid = job["series_id"]
    ctx = story.series_context(sid)
    episode = db.fetch_one("select * from episodes where id = %s", (job["episode_id"],))
    query = f"{ctx['genre_name']}. " + " ".join(s["action"] for s in episode["script"]["scenes"])[:1500]
    setups, embed_usage = shots_stage.retrieve_setups(sid, query)
    material = shots_stage.build_material(sid, episode, ctx, setups)
    standing = job["request"].get("standing")
    notes = ("Standing notes from the reviewer (apply these):\n" + standing + "\n\n") if standing else ""
    out, usage = llm.complete("director", story.build_system(shots_stage.SHOT_INSTRUCTIONS, ctx), [{"role": "user", "content": notes + material + story.notes_block(job)}],
                              schema=ShotList, fixture=lambda: shots_stage.fixture_shot_list(sid, episode), cache_system=True)
    known = {s["name"] for s in setups} | {r["name"] for r in db.fetch_all("select name from camera_setups")}
    _fail_if(shots_stage.validate_shot_list(sid, episode, out.shots, known))
    usages = shots_stage.persist_shot_list(sid, episode, out.shots, ctx["series"]["genre_id"])
    return _result([embed_usage, usage, *usages], episode=episode["number"], shots=len(out.shots))



# ---------------------------------------------------------------- voice-over and music

def _episode(job) -> dict:
    return db.fetch_one("select * from episodes where id = %s", (job["episode_id"],))


def tts_line(job: dict) -> dict:
    """One dialogue line: pronunciations applied, audio tag in square brackets in front, the character's voice."""
    sid = job["series_id"]
    line = db.fetch_one("select l.*, c.voice_id, c.name char_name from dialogue_lines l join characters c on c.id = l.character_id where l.id = %s", (job["request"]["line_id"],))
    if not line["voice_id"]:
        raise PermanentError(f"{line['char_name']} has no voice yet")
    ep = _episode(job)
    pairs = [(p["term"], p["spoken_as"]) for p in db.fetch_all("select term, spoken_as from pronunciations where series_id = %s", (sid,))]
    text = eleven.apply_pronunciations(line["text"], pairs)
    if line["audio_tag"]:
        text = f"[{line['audio_tag']}] {text}"
    language = common.get_series(sid)["language"]
    out, usage = eleven.speech(text, line["voice_id"], language, eleven.stable_seed(str(line["id"]), line["version"]))
    key = f"series/{sid}/episodes/{ep['number']:02d}/audio/line_{line['idx']}.mp3"
    storage.put_bytes(key, out["audio"], "audio/mpeg")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "line.mp3"
        path.write_bytes(out["audio"])
        seconds = ffmpeg.duration(path)   # measured from the saved file, not taken from the alignment
    db.execute("update dialogue_lines set audio_r2_key = %s, duration_seconds = %s, alignment = %s where id = %s", (key, seconds, Jsonb({"words": out["words"]}), line["id"]))
    return {"duration": seconds, "actual_cost": usage["cost_usd"], "cost_log": [{"vendor": "elevenlabs", "model": usage["model"], "units": usage["chars"], "unit_name": "char", "cost_usd": usage["cost_usd"]}]}


def music_episode(job: dict) -> dict:
    sid, ep = job["series_id"], _episode(job)
    ctx = story.series_context(sid)
    tone = ctx["bible"].tone if ctx["bible"] else "tense"
    note = job["request"].get("note") or ""
    prompt = f"Instrumental underscore for a {tone} {ctx['genre_name']} micro-drama episode. {ep['beat'].get('summary', '')[:300]} No vocals, no singing. {note}".strip()
    audio, usage = eleven.music(prompt, int(job["request"]["length_ms"]))
    key = f"series/{sid}/episodes/{ep['number']:02d}/music.mp3"
    storage.put_bytes(key, audio, "audio/mpeg")
    db.execute("update episodes set music_r2_key = %s where id = %s", (key, ep["id"]))
    return {"actual_cost": usage["cost_usd"], "cost_log": [{"vendor": "elevenlabs", "model": usage["model"], "units": usage["length_ms"] / 1000, "unit_name": "second", "cost_usd": usage["cost_usd"]}]}


# ---------------------------------------------------------------- keyframes

def _shot(job) -> dict:
    return db.fetch_one(
        "select s.*, e.number episode_number, cardinality(s.line_ids) line_count from shots s join episodes e on e.id = s.episode_id where s.id = %s", (job["shot_id"],))


def _asset(character_id, kind: str, **tags) -> dict | None:
    sql, params = "select id, r2_key_clean, face_vector::text fv, tags from character_assets where character_id = %s and kind = %s", [character_id, kind]
    for k, v in tags.items():
        sql += f" and tags->>'{k}' = %s"
        params.append(v)
    return db.fetch_one(sql + " order by tags->>'view' nulls last, created_at limit 1", params)


def keyframe_references(sid, spec: dict) -> tuple[list[bytes], list[str], dict | None]:
    """Reference images in order: each character's master portrait, their outfit image, the speaker's expression image for the
    line's emotion group, then the set plate. A kind that was not made (a limited image backend skipped it) is simply left out,
    so the master portrait alone is enough. Returns (images, labels, the first character's master asset)."""
    refs: list[bytes] = []
    labels: list[str] = []
    first_master = None
    for n, c in enumerate(spec["characters"]):
        char = db.fetch_one("select id, code from characters where series_id = %s and code = %s", (sid, c["code"]))
        master = _asset(char["id"], "master")
        if master is None:
            raise PermanentError(f"{c['code']} has no master portrait")
        first_master = first_master or master
        refs.append(look.fetch_bytes(master["r2_key_clean"]))
        labels.append(f"person {n + 1}: the person in reference image {len(refs)}")
        wardrobe = _asset(char["id"], "wardrobe", outfit=c["outfit_code"])
        if wardrobe:
            refs.append(look.fetch_bytes(wardrobe["r2_key_clean"]))
            labels.append(f"person {n + 1} wears the outfit in reference image {len(refs)}")
        if c.get("mouth_visible_speaking") or len(spec["characters"]) == 1:
            expression = _asset(char["id"], "expression", emotion_group=spec.get("emotion_group", "calm"))
            if expression:
                refs.append(look.fetch_bytes(expression["r2_key_clean"]))
                labels.append(f"person {n + 1}'s expression follows reference image {len(refs)}")
    plate = db.fetch_one("select plate_r2_key from sets where series_id = %s and code = %s and time_of_day = %s", (sid, spec["set_code"], spec["time_of_day"]))
    if plate and plate["plate_r2_key"]:
        refs.append(look.fetch_bytes(plate["plate_r2_key"]))
        labels.append(f"the place is reference image {len(refs)}")
    return refs[: images.MAX_REFERENCES], labels, first_master


def keyframe_prompt(sid, shot: dict, labels: list[str], try_n: int) -> str:
    spec = shot["spec"]
    s = db.fetch_one("select name, description from sets where series_id = %s and code = %s and time_of_day = %s", (sid, spec["set_code"], spec["time_of_day"]))
    cam = db.fetch_one("select * from camera_setups where id = %s", (shot["camera_setup_id"],)) if shot["camera_setup_id"] else None
    people = []
    for n, c in enumerate(spec["characters"]):
        w = db.fetch_one("select w.description from wardrobe_items w join characters ch on ch.id = w.character_id where w.series_id = %s and ch.code = %s and w.code = %s", (sid, c["code"], c["outfit_code"]))
        people.append(f"Person {n + 1} is at {c['position']}" + (f", wearing {w['description']}" if w else "") + ".")
    brief = common.latest_brief(sid)
    parts = [
        "A cinematic vertical still frame, 9:16, photographic, the first frame of a video shot.",
        f"Location: {s['name']}. {s['description']} Time of day: {spec['time_of_day']}.",
        (f"Camera: {cam['shot_size']} shot, {cam['angle']} angle, {cam['composition']}.") if cam else "",
        " ".join(people), f"Action: {spec['action']}",
        "Use the reference images: " + "; ".join(labels) + ".",
        ("Look: " + brief["aesthetic_note"]) if brief and brief.get("aesthetic_note") else "",
        "No text, no captions, no logos, no watermark.",
        f"Variation {try_n}." if try_n else "",
    ]
    return " ".join(p for p in parts if p)


def _enforce_keyframe_face() -> bool:
    """The face pre-check only fails a keyframe when the image backend can hold an identity; the open-source test generator cannot."""
    return os.environ.get("IMAGE_BACKEND") != "local_sd"


def image_keyframe(job: dict) -> dict:
    sid = job["series_id"]
    shot = _shot(job)
    spec = shot["spec"]
    refs, labels, master = keyframe_references(sid, spec)
    floor = get_config("qa_thresholds")["face_match_min"]   # 0.36 (guide 11.1: keyframe_face uses the same threshold)
    usages, png, score, no_face = [], b"", None, False
    for try_n in range(2):   # one more try when the face does not match the master portrait
        png, usage = images.generate_image(keyframe_prompt(sid, shot, labels, try_n), refs or None)
        usages.append(usage)
        if not look_handlers_real_faces() or master is None or master["fv"] is None:
            break
        vec = face.face_vector(png)
        if vec is None:
            no_face = True    # back to the camera or a very wide shot: passes
            break
        score = face.similarity(vec, [float(x) for x in master["fv"].strip("[]").split(",")])
        if score >= floor:
            break
    key = f"series/{sid}/episodes/{shot['episode_number']:02d}/shots/{shot['idx']}/keyframe_a{shot['attempts']}.png"
    storage.put_bytes(key, png, "image/png")
    passed = score is None or score >= floor
    if score is not None or no_face:
        with db.transaction() as conn:
            conn.execute("delete from qa_results where shot_id = %s and check_name = 'keyframe_face' and qa_round = 'precheck'", (shot["id"],))
            conn.execute("insert into qa_results (series_id, episode_id, shot_id, qa_round, check_name, passed, score, detail) values (%s,%s,%s,'precheck','keyframe_face',%s,%s,%s)",
                         (sid, shot["episode_id"], shot["id"], passed or not _enforce_keyframe_face(), score, Jsonb({"no_face": no_face, "enforced": _enforce_keyframe_face()})))
    if not passed and _enforce_keyframe_face():
        raise PermanentError(f"keyframe face similarity {score:.2f} is below {floor}")
    cache_key = work.keyframe_cache_key(spec)
    with db.transaction() as conn:
        conn.execute("update shots set keyframe_r2_key = %s, status = 'keyframe_ready' where id = %s", (key, shot["id"]))
        if shot["attempts"] == 0:   # shots sharing this cache key take the same keyframe
            for other in conn.execute("select id, spec from shots where series_id = %s and status = 'planned' and attempts = 0 and tier <> 'INSERT' and id <> %s", (sid, shot["id"])).fetchall():
                if work.keyframe_cache_key(other["spec"]) == cache_key:
                    conn.execute("update shots set keyframe_r2_key = %s, status = 'keyframe_ready' where id = %s", (key, other["id"]))
    cost = sum(u["cost_usd"] for u in usages)
    return {"keyframe": key, "face_score": score, "references": len(refs), "actual_cost": round(cost, 6),
            "cost_log": [{"vendor": "openai", "model": u["model"], "units": 1, "unit_name": "image", "cost_usd": u["cost_usd"]} for u in usages]}


def look_handlers_real_faces() -> bool:
    from app.jobs.handlers import look_handlers

    return look_handlers._real_faces()


# ---------------------------------------------------------------- shot request

def llm_shot_request(job: dict) -> dict:
    sid = job["series_id"]
    shot = _shot(job)
    model_key = job["request"].get("model_key") or MODEL_KEY_BY_TIER[shot["tier"]]
    rules, rule_usage = shots_stage.learned_rules(sid, model_key, shot["spec"]["action"])
    material = shots_stage.request_material(sid, shot, model_key, rules)
    out, usage = llm.complete("compiler", shots_stage.REQUEST_INSTRUCTIONS, [{"role": "user", "content": material + story.notes_block(job)}], schema=ShotRequest,
                              fixture=lambda: shots_stage.fixture_request(shot, model_key))
    _fail_if(shots_stage.validate_request(sid, shot, out))
    return _result([rule_usage, usage], prompt=out.prompt.strip(), camera_fixed=out.camera_fixed, rules_used=[str(r["id"]) for r in rules])


# ---------------------------------------------------------------- clips and lip-sync

def shot_audio_key(sid, shot: dict) -> str:
    return f"series/{sid}/episodes/{shot['episode_number']:02d}/shots/{shot['idx']}/shot_audio.mp3"


def build_and_store_shot_audio(sid, shot: dict, total: float) -> dict:
    """shot_audio.mp3 for a speaking shot, `total` seconds long, uploaded to R2."""
    lines = db.fetch_all("select audio_r2_key, duration_seconds from dialogue_lines where id = any(%s) order by idx", (shot["line_ids"],))
    if any(l["audio_r2_key"] is None for l in lines):
        raise PermanentError("a line of this shot has no voice-over yet")
    with tempfile.TemporaryDirectory() as tmp:
        files = []
        for i, l in enumerate(lines):
            f = Path(tmp) / f"l{i}.mp3"
            storage.get_to_file(l["audio_r2_key"], f)
            files.append(f)
        out = Path(tmp) / "shot_audio.mp3"
        info = ffmpeg.build_shot_audio(files, [float(l["duration_seconds"]) for l in lines], get_config("timing"), total, out)
        storage.put_file(shot_audio_key(sid, shot), out)
    return info


class VideoClip:
    @staticmethod
    def submit(job):
        sid, req = job["series_id"], job["request"]
        shot = _shot(job)
        if shot["tier"] == "B1":   # the avatar endpoint takes the audio: its length is the planned length
            build_and_store_shot_audio(sid, shot, float(shot["planned_duration"]))
        return falmedia.submit(job["endpoint"], media_requests.resolve_keys(req["arguments"]))

    @staticmethod
    def finalize(job):
        shot = _shot(job)
        attempt = job["request"]["attempt"]
        with tempfile.TemporaryDirectory() as tmp:
            local = falmedia.download(falmedia.video_url(job["result"]), Path(tmp) / "clip.mp4")
            info = ffmpeg.probe(local)
            key = f"series/{job['series_id']}/episodes/{shot['episode_number']:02d}/shots/{shot['idx']}/raw_a{attempt}.mp4"
            storage.put_file(key, local)
        with db.transaction() as conn:
            if (info["width"], info["height"]) != (1080, 1920):   # not a failure: the compile scales and pads
                conn.execute("insert into qa_results (series_id, episode_id, shot_id, qa_round, check_name, passed, detail) values (%s, %s, %s, 'precheck', 'resolution', false, %s)",
                             (job["series_id"], shot["episode_id"], shot["id"], Jsonb(info)))
            if needs_lipsync(shot["tier"], shot["line_count"]):
                conn.execute("update shots set raw_clip_r2_key = %s, status = 'rendered' where id = %s", (key, shot["id"]))
            else:
                conn.execute("update shots set raw_clip_r2_key = %s, final_clip_r2_key = %s, status = 'ready' where id = %s", (key, key, shot["id"]))
        return {"probe": info}


class Lipsync:
    @staticmethod
    def submit(job):
        sid = job["series_id"]
        shot = _shot(job)
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw.mp4"
            storage.get_to_file(shot["raw_clip_r2_key"], raw)
            probe = ffmpeg.probe(raw)
        info = build_and_store_shot_audio(sid, shot, round(probe["duration"], 3))   # audio length equals video length
        falmedia.check_lipsync_limits(video_seconds=probe["duration"], video_bytes=probe["bytes"], width=probe["width"], height=probe["height"],
                                      audio_seconds=info["seconds"], audio_bytes=info["bytes"])
        return falmedia.submit(job["endpoint"], media_requests.resolve_keys(job["request"]["arguments"]))

    @staticmethod
    def finalize(job):
        shot = _shot(job)
        with tempfile.TemporaryDirectory() as tmp:
            local = falmedia.download(falmedia.video_url(job["result"]), Path(tmp) / "synced.mp4")
            info = ffmpeg.probe(local)
            key = f"series/{job['series_id']}/episodes/{shot['episode_number']:02d}/shots/{shot['idx']}/synced_a{job['request']['sync_attempt']}.mp4"
            storage.put_file(key, local)
        db.execute("update shots set final_clip_r2_key = %s, status = 'ready' where id = %s", (key, shot["id"]))
        return {"probe": info}


# ---------------------------------------------------------------- inserts and the compile

def ffmpeg_insert(job: dict) -> dict:
    """A static set insert: the set plate for the shot's planned seconds with a slow zoom (guide 10.8)."""
    sid = job["series_id"]
    shot = _shot(job)
    plate_key = db.fetch_one("select plate_r2_key from sets where id = %s", (shot["set_id"],))["plate_r2_key"]
    if not plate_key:
        raise PermanentError("the set has no plate image")
    with tempfile.TemporaryDirectory() as tmp:
        plate, out = Path(tmp) / "plate.png", Path(tmp) / "insert.mp4"
        storage.get_to_file(plate_key, plate)
        media_compile.set_insert(plate, float(shot["planned_duration"]), out)
        key = f"series/{sid}/episodes/{shot['episode_number']:02d}/shots/{shot['idx']}/insert.mp4"
        storage.put_file(key, out)
    db.execute("update shots set final_clip_r2_key = %s, status = 'ready' where id = %s", (key, shot["id"]))
    return {}


def ffmpeg_compile(job: dict) -> dict:
    if dry_run() and os.environ.get("FAST_COMPILE") == "1":
        # Test-only shortcut (DRY_RUN only, set by tests/conftest.py): graph tests do not need to encode 80 seconds of 1080x1920 video.
        # The media tests unset it and run the real compile.
        from app.jobs.handlers.dryrun_stubs import ffmpeg_compile as quick

        return quick(job)
    sid, ep = job["series_id"], _episode(job)
    cut = job["request"]["cut"]
    language = common.get_series(sid)["language"]
    timing, ambience_db = get_config("timing"), float(get_config("ambience_db"))
    shots = db.fetch_all(
        "select s.*, st.ambience_r2_key from shots s left join sets st on st.id = s.set_id where s.episode_id = %s order by s.idx", (ep["id"],))
    missing = [s["idx"] for s in shots if not s["final_clip_r2_key"] or s["planned_duration"] is None]
    if not shots or missing:
        raise PermanentError(f"shots {missing or 'of this episode'} are not ready to compile")
    base = f"series/{sid}/episodes/{ep['number']:02d}"
    with tempfile.TemporaryDirectory() as tmp:
        work_dir = Path(tmp)
        videos, wavs, caption_shots, start = [], [], [], 0.0
        for s in shots:
            planned, n = float(s["planned_duration"]), s["idx"]
            clip = work_dir / f"clip{n}.mp4"
            storage.get_to_file(s["final_clip_r2_key"], clip)
            v, a = work_dir / f"v{n}.mp4", work_dir / f"a{n}.wav"
            media_compile.shot_video(clip, planned, v)
            amb = None
            if s["ambience_r2_key"]:
                amb = work_dir / f"amb{n}.mp3"
                storage.get_to_file(s["ambience_r2_key"], amb)
            if s["line_ids"]:
                rows = db.fetch_all("select text, audio_r2_key, duration_seconds, alignment from dialogue_lines where id = any(%s) order by idx", (s["line_ids"],))
                files = []
                for i, r in enumerate(rows):
                    f = work_dir / f"s{n}_{i}.mp3"
                    storage.get_to_file(r["audio_r2_key"], f)
                    files.append(f)
                speech = work_dir / f"speech{n}.mp3"
                ffmpeg.build_shot_audio(files, [float(r["duration_seconds"]) for r in rows], timing, max(planned, ffmpeg.LIPSYNC_AUDIO_SECONDS[0]), speech)
                media_compile.shot_audio_wav(planned, a, speech=speech, ambience=amb, ambience_db=ambience_db)
                caption_shots.append({"start": start, "lines": [{"text": r["text"], "duration": float(r["duration_seconds"]), "words": (r["alignment"] or {}).get("words")} for r in rows]})
            elif s["tier"] == "INSERT":
                media_compile.shot_audio_wav(planned, a, ambience=amb, ambience_db=ambience_db)
            else:
                media_compile.shot_audio_wav(planned, a, clip=clip)
            videos.append(v)
            wavs.append(a)
            start += planned
        runtime = start
        joined_v, joined_a, leveled, mixed = (work_dir / n for n in ("joined.mp4", "joined.wav", "leveled.wav", "mixed.wav"))
        media_compile.concat(videos, joined_v, work_dir)
        media_compile.concat(wavs, joined_a, work_dir)
        media_compile.level(joined_a, leveled)
        music = None
        if ep["music_r2_key"]:
            music = work_dir / "music.mp3"
            storage.get_to_file(ep["music_r2_key"], music)
        media_compile.with_music(leveled, music, runtime, mixed)
        ass = work_dir / "captions.ass"
        ass.write_text(subtitles.build_ass(language, subtitles.episode_events(language, caption_shots, timing)), encoding="utf-8")
        final = work_dir / "final.mp4"
        media_compile.encode_final(joined_v, mixed, ass, final)
        info = ffmpeg.probe(final)
        storage.put_file(f"{base}/final_v{cut}.mp4", final)
        storage.put_bytes(f"{base}/final_v{cut}.ass", ass.read_bytes(), "text/plain")
    t = 0.0
    with db.transaction() as conn:
        conn.execute("delete from edit_entries where episode_id = %s and cut_version = %s", (ep["id"], cut))
        for s in shots:
            end = t + float(s["planned_duration"])
            conn.execute("insert into edit_entries (episode_id, cut_version, shot_id, start_seconds, end_seconds) values (%s, %s, %s, %s, %s)", (ep["id"], cut, s["id"], t, end))
            t = end
        conn.execute("update episodes set final_r2_key = %s, subtitle_r2_key = %s, cut_version = %s, runtime_seconds = %s, status = 'compiled' where id = %s",
                     (f"{base}/final_v{cut}.mp4", f"{base}/final_v{cut}.ass", cut, t, ep["id"]))
    return {"runtime": t, "probe": info}


HANDLERS = {kind: SimpleNamespace(run=globals()[kind]) for kind in KINDS}
HANDLERS["video_clip"] = VideoClip
HANDLERS["lipsync"] = Lipsync
