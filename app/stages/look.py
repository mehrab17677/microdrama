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


# ------------------------------------------------------------------ prompts, validation and inputs (guide 9.5)
import re
import tempfile
from pathlib import Path

from app import db
from app.graphs import common
from app.models.look import EmotionGroupsOut, LineTagsOut, VoiceProfile
from app.text import word_count
from app.vendors import storage

MASTER_FIXED = "front-facing head-and-shoulders portrait, neutral expression, even lighting, plain background, a different person from any reference"
PLATE_FIXED = "empty location, no people"
MAX_EMOTION_GROUPS = 8

TRAIT_INSTRUCTIONS = """You describe a reference photo of a person in words so an artist can invent a DIFFERENT person with a similar feel.
Write about 60 words covering: apparent age range, build, hair, styling and mood. Never give a name, and never mention identifying
marks (scars, tattoos, birthmarks, unusual features). Plain prose, no lists, English."""

VOICE_INSTRUCTIONS = """You are a voice director. From the character definition, specify the character's speaking voice.
Fill every field. `accent`: for a Bangla series it must state a Bangladeshi accent; otherwise name a neutral accent. `design_prompt` is one
paragraph (30 to 90 words) a voice-design tool can read to create the voice: gender, age, pitch, pace, texture, energy and speech habits."""

EMOTION_INSTRUCTIONS = """Group a character's distinct free-text emotions into at most 8 groups. Each group has a one-word lower-case `name`
(for example anger, sadness, joy, fear, calm) and lists the emotions it covers. Every emotion given must appear in exactly one group."""

TAG_INSTRUCTIONS = """Turn each line's free-text emotion into a short English audio tag for a voice model: one to three words, lower case
(for example 'angry', 'whispering', 'quietly sad'). Return one tag for every line index given."""


def character_refs(series_id) -> dict:
    """character id -> brief_refs row, joined through the bible's slot_role to the intake role label."""
    bible = common.latest_bible(series_id)
    if not bible:
        return {}
    slot_by_code = {c["code"]: c.get("slot_role") for c in bible["content"].get("characters", [])}
    refs = {r["role_label"]: r for r in db.fetch_all("select * from brief_refs where series_id = %s and kind = 'character'", (series_id,))}
    return {c["id"]: refs[slot_by_code[c["code"]]] for c in db.fetch_all("select id, code from characters where series_id = %s", (series_id,)) if slot_by_code.get(c["code"]) in refs}


def fetch_bytes(key: str) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "f"
        storage.get_to_file(key, path)
        return path.read_bytes()


def aesthetic_images(series_id, limit: int = 3) -> list[bytes]:
    return [fetch_bytes(r["r2_key"]) for r in db.fetch_all("select r2_key from brief_refs where series_id = %s and kind = 'aesthetic' order by created_at limit %s", (series_id, limit))]


def trait_sheet_for(series_id, character_id) -> str:
    """The trait sheet the model wrote for this character's reference photo (latest successful job), or ''."""
    ref = character_refs(series_id).get(character_id)
    if not ref:
        return ""
    row = db.fetch_one("select result from jobs where series_id = %s and kind = 'llm_trait_sheet' and status = 'succeeded' and request->>'ref_id' = %s order by finished_at desc limit 1",
                       (series_id, str(ref["id"])))
    return ((row or {}).get("result") or {}).get("trait_sheet", "")


def validate_trait_sheet(text: str) -> list[str]:
    n = word_count(text)
    return [] if 30 <= n <= 90 else [f"the trait sheet is {n} words; write about 60 (30 to 90)"]


def validate_voice_profile(profile: VoiceProfile, language: str) -> list[str]:
    problems = [f"{name} must not be empty" for name, value in profile.model_dump().items() if not str(value).strip()]
    if language == "bn" and "bangladesh" not in profile.accent.lower():
        problems.append("for a Bangla series the accent must state a Bangladeshi accent")
    if profile.design_prompt.strip() and not 20 <= word_count(profile.design_prompt) <= 120:
        problems.append(f"design_prompt is {word_count(profile.design_prompt)} words; write one paragraph of 30 to 90")
    return problems


def emotion_map(groups: EmotionGroupsOut, emotions: list[str]) -> tuple[dict[str, str], list[str]]:
    """(emotion -> group name, problems)."""
    problems: list[str] = []
    if len(groups.groups) > MAX_EMOTION_GROUPS:
        problems.append(f"use at most {MAX_EMOTION_GROUPS} groups, not {len(groups.groups)}")
    mapping: dict[str, str] = {}
    for g in groups.groups:
        if not re.fullmatch(r"[a-z]+", g.name):
            problems.append(f"group name {g.name!r} must be one lower-case word")
        for e in g.emotions:
            if e in mapping:
                problems.append(f"the emotion {e!r} appears in two groups")
            mapping[e] = g.name
    missing = [e for e in emotions if e not in mapping]
    if missing:
        problems.append("these emotions are in no group: " + ", ".join(missing[:8]))
    return {e: mapping[e] for e in emotions if e in mapping}, problems


def validate_line_tags(tags: LineTagsOut, wanted: list[int]) -> tuple[dict[int, str], list[str]]:
    problems: list[str] = []
    got: dict[int, str] = {}
    for t in tags.tags:
        tag = t.tag.strip().lower()
        if not 1 <= word_count(tag) <= 3:
            problems.append(f"line {t.idx}: the tag {t.tag!r} must be one to three words")
        elif t.idx not in wanted:
            problems.append(f"line {t.idx} was not asked for")
        else:
            got[t.idx] = tag
    missing = [i for i in wanted if i not in got]
    if missing:
        problems.append("no tag for lines: " + ", ".join(map(str, missing[:10])))
    return got, problems


def master_prompt(character: dict, trait_sheet: str, aesthetic_note: str | None) -> str:
    parts = [character["visual_description"].strip()]
    if trait_sheet:
        parts.append(trait_sheet.strip())
    if aesthetic_note:
        parts.append("Look: " + aesthetic_note.strip())
    parts.append(MASTER_FIXED)
    return "\n".join(parts)


ANGLE_WORDS = {"front": "front view", "three_quarter_left": "three-quarter view turned to the left", "three_quarter_right": "three-quarter view turned to the right",
               "profile": "side profile view", "back": "view from behind"}
VIEW_WORDS = {"full_body_front": "full body, front view", "three_quarter": "three-quarter view"}


def pack_prompt(character: dict, kind: str, tags: dict, outfit_description: str | None) -> str:
    """The text part of a pack image prompt. The prime (master) portrait goes in as the reference, so every
    sheet shows the same person: full-body views for turnaround, wardrobe and pose; tight face close-ups
    for expressions."""
    base = f"The same person as the reference prime portrait. {character['visual_description'].strip()} "
    if kind == "turnaround":
        angle = ANGLE_WORDS.get(tags.get("angle"), tags.get("angle", "front view"))
        return base + f"Character turnaround: full body from head to toe, {angle}, standing neutrally, arms relaxed, plain light background."
    if kind == "expression":
        group = str(tags.get("emotion_group", "calm")).replace("_", " ")
        return base + f"Tight close-up of the face, filling the frame, showing a {group} expression, plain background."
    if kind == "wardrobe":
        view = VIEW_WORDS.get(tags.get("view"), tags.get("view", "front view"))
        return base + f"Full body from head to toe wearing: {outfit_description or tags.get('outfit', '')}. {view}, plain background."
    if kind == "pose":
        return base + f"Full body from head to toe, {str(tags.get('pose', 'standing')).replace('_', ' ')} pose, plain background."
    raise ValueError(f"unknown pack kind {kind!r}")


def plate_prompt(set_row: dict, aesthetic_note: str | None) -> str:
    parts = [f"{set_row['name']}: {set_row['description']}", f"Time of day: {set_row['time_of_day']}."]
    if aesthetic_note:
        parts.append("Look: " + aesthetic_note.strip())
    parts.append(PLATE_FIXED)
    return " ".join(parts)
