"""Real handlers for the look stage (guide 9.5): trait sheets, voice profiles, emotion groups and line tags,
master portraits, pack images, set plates, ambience and voice design.

Images and voices go through app/vendors (OpenAI images / ElevenLabs by default; open-source test backends
when IMAGE_BACKEND=local_sd / VOICE_BACKEND=local are set). Review images are the clean image plus a caption bar
drawn here; the image model is never asked to draw text.
"""
from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

from psycopg.types.json import Jsonb

from app import db
from app.config import dry_run
from app.graphs import common
from app.jobs.errors import PermanentError
from app.media import review
from app.models.look import EmotionGroup, EmotionGroupsOut, LineTag, LineTagsOut, VoiceProfile
from app.qa import face
from app.stages import look
from app.stages.ingest import _cost_rows
from app.vendors import eleven, images, llm, storage

KINDS = ["llm_trait_sheet", "llm_voice_profile", "llm_audio_tag", "image_master", "image_pack", "image_set_plate", "sfx_ambience", "voice_design"]

TRAIT_FIXTURE = ("Adult in their late twenties, medium build, dark shoulder-length hair worn loose, plain dark jacket over a light blouse, "
                 "minimal styling and no jewellery. A calm, watchful, composed mood with quiet confidence and a steady gaze, the posture "
                 "of someone who listens before speaking.")
EMOTION_GROUP_FIXTURE = {"neutral": "calm", "angry": "anger", "sad": "sadness"}


def _usage_row(u: dict) -> dict | None:
    """A cost_log row for an image or audio usage (language-model and embedding usages go through _cost_rows)."""
    if not u.get("cost_usd"):
        return None                                     # free (local backend, or no price published)
    model = str(u["model"])
    vendor = "local" if model.startswith("local:") else ("openai" if "image" in model else "elevenlabs")
    unit = "image" if "image" in model else ("second" if "sound" in model else "char")
    return {"vendor": vendor, "model": model, "units": 1, "unit_name": unit, "cost_usd": u["cost_usd"]}


def _result(usages: list[dict], **extra) -> dict:
    usages = [u for u in usages if u]
    token_usages = [u for u in usages if "tokens_in" in u or "tokens" in u]
    rows = _cost_rows(token_usages) + [r for r in (_usage_row(u) for u in usages if u not in token_usages) if r]
    return {**extra, "actual_cost": round(sum(u.get("cost_usd", 0.0) for u in usages), 6), "cost_log": rows}


def _fail_if(problems: list[str]) -> None:
    if problems:
        raise PermanentError(f"{len(problems)} problem(s): " + "; ".join(problems[:10]))


def _previous(job: dict) -> str:
    from app.stages import story

    return story.notes_block(job)


# ---------------------------------------------------------------- text stages

def llm_trait_sheet(job: dict) -> dict:
    ref = db.fetch_one("select * from brief_refs where id = %s", (job["request"]["ref_id"],))
    if ref is None:
        raise PermanentError("the reference photo no longer exists")
    photo = look.fetch_bytes(ref["r2_key"])
    text, usage = llm.complete("writer", look.TRAIT_INSTRUCTIONS, [{"role": "user", "content": f"Story role: {ref['role_label']}. Describe the person in the photo." + _previous(job)}],
                               images=[photo], fixture=TRAIT_FIXTURE)
    text = text.strip()
    _fail_if(look.validate_trait_sheet(text))
    return _result([usage], trait_sheet=text, ref_id=str(ref["id"]))


def _voice_fixture(language: str, is_lead: bool) -> VoiceProfile:
    return VoiceProfile(voice_gender="female", voice_age="adult", pitch="low", pace="measured", accent="Bangladeshi" if language == "bn" else "neutral",
                        texture="warm", energy="restrained", speech_habits="pauses before hard truths",
                        design_prompt="A calm adult woman with a warm, low, measured voice and a restrained energy, who pauses before saying hard truths and never raises her voice.")


def llm_voice_profile(job: dict) -> dict:
    sid = job["series_id"]
    char = db.fetch_one("select * from characters where id = %s", (job["request"]["character_id"],))
    series = common.get_series(sid)
    bible_char = next((c for c in (common.latest_bible(sid) or {"content": {"characters": []}})["content"]["characters"] if c["code"] == char["code"]), {})
    material = (f"Character {char['code']} {char['name']}: {char['role']}, age {bible_char.get('age', 'adult')}, personality: {bible_char.get('personality', '')}.\n"
                f"Look: {char['visual_description']}\nDialogue language: {series['language']}." + _previous(job))
    profile, usage = llm.complete("writer", look.VOICE_INSTRUCTIONS, [{"role": "user", "content": material}], schema=VoiceProfile,
                                  fixture=lambda: _voice_fixture(series["language"], char["is_lead"]))
    _fail_if(look.validate_voice_profile(profile, series["language"]))
    db.execute("update characters set voice_profile = %s where id = %s", (Jsonb(profile.model_dump()), char["id"]))
    return _result([usage], character=char["code"])


def llm_audio_tag(job: dict) -> dict:
    req = job["request"]
    if req["purpose"] == "emotion_groups":
        return _emotion_groups(job)
    return _line_tags(job)


def _emotion_groups(job: dict) -> dict:
    cid = job["request"]["character_id"]
    emotions = [r["emotion"] for r in db.fetch_all("select distinct emotion from dialogue_lines where character_id = %s order by emotion", (cid,))]
    if not emotions:
        db.execute("update characters set emotion_groups = %s where id = %s", (Jsonb({"neutral": "calm"}), cid))
        return {"groups": 0}
    out, usage = llm.complete("copy", look.EMOTION_INSTRUCTIONS, [{"role": "user", "content": "Emotions:\n- " + "\n- ".join(emotions) + _previous(job)}], schema=EmotionGroupsOut,
                              fixture=lambda: EmotionGroupsOut(groups=[EmotionGroup(name=g, emotions=[e for e in emotions if EMOTION_GROUP_FIXTURE.get(e, "calm") == g])
                                                                        for g in sorted({EMOTION_GROUP_FIXTURE.get(e, "calm") for e in emotions})]))
    mapping, problems = look.emotion_map(out, emotions)
    _fail_if(problems)
    db.execute("update characters set emotion_groups = %s where id = %s", (Jsonb(mapping), cid))
    return _result([usage], groups=len(set(mapping.values())))


def _line_tags(job: dict) -> dict:
    lines = db.fetch_all("select idx, emotion from dialogue_lines where episode_id = %s and audio_tag is null order by idx", (job["episode_id"],))
    if not lines:
        return {"tagged": 0}
    note = job["request"].get("note") or ""
    material = "Lines:\n" + "\n".join(f"{l['idx']}: {l['emotion']}" for l in lines) + (f"\n\nReviewer note: {note}" if note else "") + _previous(job)
    out, usage = llm.complete("copy", look.TAG_INSTRUCTIONS, [{"role": "user", "content": material}], schema=LineTagsOut,
                              fixture=lambda: LineTagsOut(tags=[LineTag(idx=l["idx"], tag=l["emotion"].strip().lower().split(",")[0][:30] or "neutral") for l in lines]))
    tags, problems = look.validate_line_tags(out, [l["idx"] for l in lines])
    _fail_if(problems)
    with db.transaction() as conn:
        for idx, tag in tags.items():
            conn.execute("update dialogue_lines set audio_tag = %s where episode_id = %s and idx = %s", (tag, job["episode_id"], idx))
    return _result([usage], tagged=len(tags))


# ---------------------------------------------------------------- images

def _real_faces() -> bool:
    """Do generated images contain real faces? Not with the dry-run fixture image."""
    return not dry_run() or os.environ.get("IMAGE_BACKEND") == "local_sd"


def _save_pair(sid, folder: str, asset_id, png: bytes, title: str, subtitle: str) -> tuple[str, str]:
    base = f"series/{sid}/characters/{folder}/{asset_id}"
    storage.put_bytes(base + "_clean.png", png, "image/png")                      # clean: no text
    storage.put_bytes(base + "_review.png", review.caption_bar(png, title, subtitle), "image/png")
    return base + "_review.png", base + "_clean.png"


def image_master(job: dict) -> dict:
    sid, req = job["series_id"], job["request"]
    char = db.fetch_one("select * from characters where id = %s", (req["character_id"],))
    brief = common.latest_brief(sid)
    refs = []
    ref = look.character_refs(sid).get(char["id"])
    if ref and req.get("use_photo"):                       # the first two tries also see the photo; the third does not
        refs.append(look.fetch_bytes(ref["r2_key"]))
    refs += look.aesthetic_images(sid)
    prompt = look.master_prompt(char, look.trait_sheet_for(sid, char["id"]), brief["aesthetic_note"])
    png, usage = images.generate_image(prompt, references=refs or None)
    vector = face.face_vector(png)
    if vector is None:
        if _real_faces():
            raise PermanentError("no face was found in the master portrait")
        vector = face.stub_vector(f"{char['code']}:{req['try']}")        # dry run: the fixture image has no face
    asset_id = uuid.uuid4()
    review_key, clean_key = _save_pair(sid, char["code"], asset_id, png, char["name"], "master portrait")
    db.execute(
        "insert into character_assets (id, series_id, character_id, kind, tags, r2_key_review, r2_key_clean, face_vector, version) values (%s, %s, %s, 'master', '{}'::jsonb, %s, %s, %s::vector, %s)",
        (asset_id, sid, char["id"], review_key, clean_key, db.to_vector(vector), req["try"]))
    return _result([usage], asset_id=str(asset_id))


def image_pack(job: dict) -> dict:
    sid, req = job["series_id"], job["request"]
    char = req["character_id"] and db.fetch_one("select * from characters where id = %s", (req["character_id"],))
    asset_id = uuid.uuid4()
    if char:
        master = db.fetch_one("select r2_key_clean, face_vector::text fv from character_assets where character_id = %s and kind = 'master' order by version desc limit 1", (char["id"],))
        if master is None:
            raise PermanentError("the character has no master portrait yet")
        outfit = None
        if req["kind"] == "wardrobe":
            row = db.fetch_one("select description from wardrobe_items where character_id = %s and code = %s", (char["id"], req["tags"].get("outfit")))
            outfit = row and row["description"]
        prompt = look.pack_prompt(char, req["kind"], req["tags"], outfit)
        png, usage = images.generate_image(prompt, references=[look.fetch_bytes(master["r2_key_clean"])])
        vector = face.face_vector(png)
        if vector is None and not _real_faces():
            vector = [float(x) for x in master["fv"].strip("[]").split(",")] if master["fv"] else None   # dry run: same face as the master
        title, folder = char["name"], char["code"]
        subtitle = " · ".join([req["kind"], *[str(v).replace("_", " ") for v in req["tags"].values()]])
    else:                                                                         # the cast line-up
        masters = db.fetch_all("select c.code, c.name, c.visual_description, a.r2_key_clean from character_assets a join characters c on c.id = a.character_id"
                               " where a.series_id = %s and a.kind = 'master' order by c.code", (sid,))
        prompt = ("Cast line-up: all of these characters standing side by side at true relative height, full body, plain background. "
                  + " ".join(f"{m['code']}: {m['visual_description'].strip()}" for m in masters))
        png, usage = images.generate_image(prompt, references=[look.fetch_bytes(m["r2_key_clean"]) for m in masters[:16]] or None)
        vector, title, folder, subtitle = None, "Cast line-up", "lineup", ""
    review_key, clean_key = _save_pair(sid, folder, asset_id, png, title, subtitle)
    db.execute(
        "insert into character_assets (id, series_id, character_id, kind, tags, r2_key_review, r2_key_clean, face_vector) values (%s, %s, %s, %s, %s, %s, %s, %s::vector)",
        (asset_id, sid, char and char["id"], req["kind"], Jsonb(req["tags"]), review_key, clean_key, vector and db.to_vector(vector)))
    return _result([usage], asset_id=str(asset_id))


def image_set_plate(job: dict) -> dict:
    sid = job["series_id"]
    s = db.fetch_one("select * from sets where id = %s", (job["request"]["set_id"],))
    brief = common.latest_brief(sid)
    png, usage = images.generate_image(look.plate_prompt(s, brief["aesthetic_note"]), references=look.aesthetic_images(sid) or None)
    key = f"series/{sid}/sets/{s['code']}_{s['time_of_day']}.png"
    storage.put_bytes(key, png, "image/png")
    db.execute("update sets set plate_r2_key = %s where id = %s", (key, s["id"]))
    return _result([usage])


# ---------------------------------------------------------------- audio

def sfx_ambience(job: dict) -> dict:
    sid, code = job["series_id"], job["request"]["set_code"]
    s = db.fetch_one("select name, description from sets where series_id = %s and code = %s limit 1", (sid, code))
    audio, usage = eleven.sound_effect(f"Ambient background sound of {s['name']}: {s['description']}", 30, loop=True)
    key = f"series/{sid}/sets/{code}_ambience.mp3"
    storage.put_bytes(key, audio, "audio/mpeg")
    db.execute("update sets set ambience_r2_key = %s where series_id = %s and code = %s", (key, sid, code))
    return _result([usage])


def voice_design(job: dict) -> dict:
    """Design a voice from the character's VoiceProfile, preview it on one of their own lines, and save it."""
    sid = job["series_id"]
    char = db.fetch_one("select * from characters where id = %s", (job["request"]["character_id"],))
    if not char["voice_profile"]:
        raise PermanentError("the character has no voice profile yet")
    lines, text = [], ""
    for l in db.fetch_all("select text from dialogue_lines where character_id = %s order by created_at, idx limit 12", (char["id"],)):
        lines.append(l["text"])
        text = " ".join(lines)
        if len(text) >= 100:
            break
    previews, usage = eleven.design_voice(char["voice_profile"]["design_prompt"], text[:900] or None)
    series = common.get_series(sid)
    voice_id = eleven.save_voice(previews[0]["generated_voice_id"], f"{series['title'][:24]}-{char['code']}", char["voice_profile"]["design_prompt"])
    asset_id = uuid.uuid4()
    key = f"series/{sid}/characters/{char['code']}/{asset_id}_voice.mp3"
    storage.put_bytes(key, previews[0]["audio"], "audio/mpeg")
    db.execute("insert into character_assets (id, series_id, character_id, kind, tags, r2_key_clean) values (%s, %s, %s, 'voice_sample', %s, %s)",
               (asset_id, sid, char["id"], Jsonb({"voice_id": voice_id, "sample_text": text[:200]}), key))
    return _result([usage], voice_id=voice_id, asset_id=str(asset_id))


HANDLERS = {kind: SimpleNamespace(run=globals()[kind]) for kind in KINDS}
