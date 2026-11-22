"""Shot-list stage (guide 9.6): context for the Lead Director, the code checks, and persistence.

The model plans 8 to 12 shots per episode. Everything that can be checked in code is checked here before
anything is stored (counts, line order, codes, wardrobe, speaking rules), so a bad plan fails the job and the
graph retries it with the problems listed.
"""
from __future__ import annotations

import json

from psycopg.types.json import Jsonb

from app import db
from app.config import get_config
from app.graphs import common
from app.media.routing import tier_for
from app.models.shots import CameraSetupDef, ShotCharacter, ShotList, ShotSpec
from app.vendors import images

SPEAKING_TYPES = ("speaking_closeup", "speaking_wider", "hero")
SHOT_TYPES = ("non_speaking", "speaking_closeup", "speaking_wider", "hero", "static_insert")
LIKELY_MODEL_KEY = "kling_v3_pro"   # tiers A, B2 and C, the most common ones, decide which setups to leave out
SETUP_LIMIT = 30

SHOT_INSTRUCTIONS = """You are the Lead Director of a vertical micro-drama (9:16, 75 to 90 seconds per episode).
Break one episode's script into 8 to 12 shots and return a ShotList.

Rules:
- The first shot is the hook (is_hook = true) and the last is the cliffhanger (is_cliffhanger = true).
- Every dialogue line index is used exactly once, in order, across the shots' line_indexes. A shot that speaks lists its lines; every other shot has an empty line_indexes.
- shot_type is one of: non_speaking (no dialogue), speaking_closeup (one speaker, tight on the face), speaking_wider (one speaker, wider framing), hero (a key dramatic moment, may speak), static_insert (an empty set view).
- A speaking shot has EXACTLY ONE character with mouth_visible_speaking = true, and every line in the shot belongs to that character. Break the dialogue into a new shot whenever the speaker changes.
- Use only character, outfit, set and time-of-day codes that are given. Each character's outfit_code must be the one the wardrobe plan gives for that scene.
- camera_setup_name is the name of one of the listed camera setups. If none fits, invent a new one: put its full definition in new_setup and use its name; otherwise new_setup is null.
- est_seconds (2 to 10) is your estimate of how long a shot with NO dialogue should run; for shots with dialogue it is ignored.
- Spoken shots are timed by the voice-over, so keep each speaking shot to a few lines. Put non-speaking shots (reactions, objects, places, movement) between them so the episode breathes.
- sounds lists the sound effects heard in the shot, as short phrases. Never put music or speech there.
- action describes what the camera sees and what people do. Never put on-screen text in it.
- placement_kind is the placement from the plan when the shot carries one, otherwise null.
- Brand placements (only when the episode material lists one): the brand's real product appears INSIDE a normal scene, drawn by the video model. Put the placement on ONE shot of the scene where it is natural: set placement_kind to the planned kind. That shot is a non_speaking shot or a speaking_wider shot (never a close-up, a hero shot or a static_insert), shows the product being used the way the story needs, and runs long enough for it to be read (est_seconds of at least the minimum given, for a non-speaking shot). Describe what the people do with the product in action, without naming the brand or any printed text. A spoken placement is placement_kind 'spoken' on the shot that carries the brand wording."""


# ---------------------------------------------------------------- context

def episode_lines(episode_id) -> list[dict]:
    return db.fetch_all(
        "select l.idx, l.text, l.emotion, c.code from dialogue_lines l join characters c on c.id = l.character_id where l.episode_id = %s order by l.idx", (episode_id,))


def wardrobe_for(sid, number: int) -> dict[tuple[str, int], str]:
    """(character code, scene index) -> outfit code, from the wardrobe plan."""
    out: dict[tuple[str, int], str] = {}
    for w in db.fetch_all("select w.code, w.scenes, c.code char_code from wardrobe_items w join characters c on c.id = w.character_id where w.series_id = %s", (sid,)):
        for ref in w["scenes"] or []:
            if ref.get("episode") == number:
                out[(w["char_code"], int(ref["scene"]))] = w["code"]
    return out


def outfit_codes(sid) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for w in db.fetch_all("select w.code, c.code char_code from wardrobe_items w join characters c on c.id = w.character_id where w.series_id = %s", (sid,)):
        out.setdefault(w["char_code"], set()).add(w["code"])
    return out


def retrieve_setups(sid, query: str) -> tuple[list[dict], dict | None]:
    """The 30 camera setups nearest to the genre and scene text, leaving out any that avoid the likely model."""
    s = common.get_series(sid)
    (vec,), usage = images.embed([query])
    rows = db.fetch_all(
        "select name, shot_size, angle, movement, composition, best_for from camera_setups"
        " where embedding is not null and not (%s = any(avoid_models)) and (genre_id is null or genre_id = %s)"
        " order by embedding <=> %s::vector limit %s", (LIKELY_MODEL_KEY, s["genre_id"], db.to_vector(vec), SETUP_LIMIT))
    return rows, usage


def build_material(sid, episode: dict, ctx: dict, setups: list[dict]) -> str:
    script = episode["script"]
    lines = episode_lines(episode["id"])
    by_idx = {l["idx"]: l for l in lines}
    plan = wardrobe_for(sid, episode["number"])
    scenes = []
    cursor = 0
    for scene in script["scenes"]:
        rows = []
        for _ in scene["lines"]:
            l = by_idx[cursor]
            rows.append(f"  [{l['idx']}] {l['code']} ({l['emotion']}): {l['text']}")
            cursor += 1
        wear = {c: o for (c, sc), o in plan.items() if sc == scene["index"]}
        scenes.append(f"Scene {scene['index']} in {scene['set_code']} at {scene['time_of_day']}. Action: {scene['action']}\n  Outfits: {json.dumps(wear)}\n" + "\n".join(rows))
    sets = db.fetch_all("select code, name, time_of_day from sets where series_id = %s order by code, time_of_day", (sid,))
    chars = db.fetch_all("select code, name from characters where series_id = %s order by code", (sid,))
    return "\n\n".join([placement_block(sid, episode["number"])] * bool(episode_placements(sid, episode["number"])) + [
        f"Episode {episode['number']}. Hook: {script['hook_action']}  Cliffhanger: {script['cliffhanger_action']}",
        "Characters: " + ", ".join(f"{c['code']} ({c['name']})" for c in chars),
        "Sets: " + ", ".join(f"{s['code']} {s['name']} [{s['time_of_day']}]" for s in sets),
        "Script:\n" + "\n\n".join(scenes),
        "Camera setups you may use (name | size | angle | movement | composition | best for):\n"
        + ("\n".join(f"- {s['name']} | {s['shot_size']} | {s['angle']} | {s['movement']} | {s['composition']} | {s['best_for']}" for s in setups) or "(the library is empty: define new setups)"),
    ])


def episode_placements(sid, number: int) -> list[dict]:
    return db.fetch_all("select kind, description from placements where series_id = %s and episode_number = %s order by created_at", (sid, number))


def placement_block(sid, number: int) -> str:
    from app import placement

    brief = placement.brief(sid) or {}
    minimum = float(brief.get("min_exposure_seconds") or 3)
    lines = []
    for p in episode_placements(sid, number):
        extra = f" (the shot needs est_seconds of at least {min(10, minimum + 1):g} so the product can be read)" if p["kind"] in placement.VISUAL else ""
        lines.append(f"- {p['kind']}{extra}: {p['description']}")
    product = f"Product: {brief.get('product', '')}. How it is used: {str(brief.get('product_description', ''))[:400]}"
    return "Brand placement planned for this episode (the shot list must include it):\n" + "\n".join(lines) + "\n" + product


# ---------------------------------------------------------------- checks

def validate_shot_list(sid, episode: dict, shots: list[ShotSpec], known_setups: set[str]) -> list[str]:
    """Every problem at once. The graph's own validate_shot_rows repeats the structural checks on stored rows."""
    lo, hi = get_config("shot_count_range")
    problems: list[str] = []
    if not lo <= len(shots) <= hi:
        problems.append(f"an episode needs {lo} to {hi} shots, not {len(shots)}")
    if not shots:
        return problems
    if not shots[0].is_hook:
        problems.append("the first shot must be the hook")
    if not shots[-1].is_cliffhanger:
        problems.append("the last shot must be the cliffhanger")
    lines = episode_lines(episode["id"])
    owner = {l["idx"]: l["code"] for l in lines}
    used = [i for s in shots for i in s.line_indexes]
    if used != list(range(len(lines))):
        problems.append(f"every dialogue line must be used once and in order (lines 0..{len(lines) - 1}); got {used}")
    chars = {r["code"] for r in db.fetch_all("select code from characters where series_id = %s", (sid,))}
    sets = {(r["code"], r["time_of_day"]) for r in db.fetch_all("select code, time_of_day from sets where series_id = %s", (sid,))}
    outfits = outfit_codes(sid)
    plan = wardrobe_for(sid, episode["number"])
    from app import placement

    planned_kinds = {p["kind"] for p in episode_placements(sid, episode["number"])}
    minimum = float((placement.brief(sid) or {}).get("min_exposure_seconds") or 3)
    for kind in sorted(planned_kinds):
        if not any(s.placement_kind == kind for s in shots):
            problems.append(f"the episode's {kind} placement is missing: add a shot with placement_kind '{kind}'")
    for n, s in enumerate(shots):
        tag = f"shot {n}"
        if s.idx != n:
            problems.append(f"{tag}: idx must be {n}, not {s.idx}")
        if s.shot_type not in SHOT_TYPES:
            problems.append(f"{tag}: unknown shot_type {s.shot_type!r}")
            continue
        if (s.set_code, s.time_of_day) not in sets:
            problems.append(f"{tag}: the set {s.set_code} at {s.time_of_day} does not exist")
        speaking = bool(s.line_indexes)
        if speaking and s.shot_type not in SPEAKING_TYPES:
            problems.append(f"{tag}: it has lines, so its shot_type must be one of {', '.join(SPEAKING_TYPES)}")
        if not speaking and s.shot_type in ("speaking_closeup", "speaking_wider"):
            problems.append(f"{tag}: a {s.shot_type} shot needs lines")
        if s.shot_type == "static_insert" and (speaking or s.characters):
            problems.append(f"{tag}: a static_insert shows an empty set: no characters and no lines")
        if not speaking and not 2 <= s.est_seconds <= 10 and s.shot_type != "static_insert":
            problems.append(f"{tag}: est_seconds must be between 2 and 10, not {s.est_seconds}")
        for c in s.characters:
            if c.code not in chars:
                problems.append(f"{tag}: unknown character {c.code}")
            elif c.outfit_code not in outfits.get(c.code, set()):
                problems.append(f"{tag}: {c.code} has no outfit {c.outfit_code}")
            elif (c.code, s.scene_index) in plan and plan[(c.code, s.scene_index)] != c.outfit_code:
                problems.append(f"{tag}: the wardrobe plan has {c.code} in {plan[(c.code, s.scene_index)]} in scene {s.scene_index}, not {c.outfit_code}")
        if speaking:
            speakers = [c for c in s.characters if c.mouth_visible_speaking]
            if len(speakers) != 1:
                problems.append(f"{tag}: it needs exactly one character with mouth_visible_speaking, not {len(speakers)}")
            elif any(owner.get(i) != speakers[0].code for i in s.line_indexes):
                problems.append(f"{tag}: every line in it must belong to {speakers[0].code}")
        elif any(c.mouth_visible_speaking for c in s.characters):
            problems.append(f"{tag}: nobody speaks in it, so mouth_visible_speaking must be false")
        if s.placement_kind and s.placement_kind not in planned_kinds:
            problems.append(f"{tag}: placement_kind {s.placement_kind!r} is not in this episode's placement plan")
        if s.placement_kind in placement.VISUAL:
            if s.shot_type not in ("non_speaking", "speaking_wider"):
                problems.append(f"{tag}: a brand shot is a non_speaking or speaking_wider shot, not {s.shot_type}")
            if s.shot_type == "non_speaking" and s.est_seconds < min(10, minimum + 1):
                problems.append(f"{tag}: the product needs at least {min(10, minimum + 1):g} seconds on screen to be read, not {s.est_seconds}")
            if s.placement_kind in ("product_in_use", "product_shown") and not s.characters:
                problems.append(f"{tag}: a {s.placement_kind} shot needs the character who handles the product")
        if s.placement_kind == "spoken" and not speaking:
            problems.append(f"{tag}: a spoken placement is on a shot with lines")
        name = s.camera_setup_name.strip()
        if s.new_setup is not None:
            if s.new_setup.name.strip() != name:
                problems.append(f"{tag}: camera_setup_name must equal new_setup.name")
        elif name not in known_setups:
            problems.append(f"{tag}: camera setup {name!r} is not in the list; define it in new_setup")
    return problems


# ---------------------------------------------------------------- persistence

def emotion_group_for(sid, speaker_code: str | None, emotion: str | None) -> str:
    """The speaker's group for the line's free-text emotion; 'calm' when there is none."""
    if speaker_code and emotion:
        row = db.fetch_one("select emotion_groups from characters where series_id = %s and code = %s", (sid, speaker_code))
        groups = (row or {}).get("emotion_groups") or {}
        if emotion in groups:
            return groups[emotion]
    return "calm"


def persist_shot_list(sid, episode: dict, shots: list[ShotSpec], genre_id) -> list[dict]:
    """Replace the episode's shots, insert new camera setups (with an embedding), set status 'shots_planned'.
    Returns the embedding usages."""
    lines = {l["idx"]: l for l in episode_lines(episode["id"])}
    line_rows = {r["idx"]: r["id"] for r in db.fetch_all("select idx, id from dialogue_lines where episode_id = %s", (episode["id"],))}
    sets = {(r["code"], r["time_of_day"]): r["id"] for r in db.fetch_all("select id, code, time_of_day from sets where series_id = %s", (sid,))}
    chars = {r["code"]: r["id"] for r in db.fetch_all("select id, code from characters where series_id = %s", (sid,))}
    usages: list[dict] = []
    new_defs = {s.new_setup.name.strip(): s.new_setup for s in shots if s.new_setup is not None}
    existing = {r["name"]: r["id"] for r in db.fetch_all("select id, name from camera_setups where name = any(%s)", (list(new_defs) or [""],))}
    new_ids: dict[str, object] = dict(existing)
    for name, d in new_defs.items():
        if name in existing:
            continue
        (vec,), usage = images.embed([f"{d.name}. {d.shot_size}, {d.angle}, {d.movement}. {d.composition}. Best for {d.best_for}"])
        usages.append(usage)
        new_ids[name] = db.fetch_one(
            "insert into camera_setups (name, shot_size, angle, movement, composition, best_for, genre_id, origin_series_id, embedding)"
            " values (%s,%s,%s,%s,%s,%s,%s,%s,%s::vector) returning id",
            (d.name.strip(), d.shot_size, d.angle, d.movement, d.composition, d.best_for, genre_id, sid, db.to_vector(vec)))["id"]
    library = {r["name"]: r["id"] for r in db.fetch_all("select id, name from camera_setups")}
    with db.transaction() as conn:
        conn.execute("delete from shots where episode_id = %s", (episode["id"],))
        for s in shots:
            speaker = next((c for c in s.characters if c.mouth_visible_speaking), None) if s.line_indexes else None
            first_emotion = lines[s.line_indexes[0]]["emotion"] if s.line_indexes else None
            group = emotion_group_for(sid, speaker.code if speaker else (s.characters[0].code if s.characters else None), first_emotion)
            spec = {**s.model_dump(), "emotion_group": group, **({"brand": True} if s.placement_kind in ("product_in_use", "product_shown", "product_ambient", "logo_in_scene") else {})}
            tier = tier_for(s.shot_type)
            conn.execute(
                "insert into shots (series_id, episode_id, idx, shot_type, tier, camera_setup_id, set_id, spec, line_ids, speaker_character_id, status)"
                " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (sid, episode["id"], s.idx, s.shot_type, tier, new_ids.get(s.camera_setup_name.strip()) or library.get(s.camera_setup_name.strip()),
                 sets[(s.set_code, s.time_of_day)], Jsonb(spec), [line_rows[i] for i in s.line_indexes], chars[speaker.code] if speaker else None,
                 "keyframe_ready" if tier == "INSERT" else "planned"))   # an insert has no keyframe: it goes straight to ffmpeg_insert
        conn.execute("update episodes set status = 'shots_planned' where id = %s", (episode["id"],))
    from app import placement

    placement.link_shots(sid, episode["number"])
    return usages


# ---------------------------------------------------------------- DRY_RUN fixture

def fixture_shot_list(sid, episode: dict) -> ShotList:
    """A valid shot list built from the real script: consecutive lines of one speaker become one speaking shot
    (alternating close-up and wider), with a non-speaking shot before each while the count stays within range."""
    lo, hi = get_config("shot_count_range")
    lines = episode_lines(episode["id"])
    runs: list[list[dict]] = []
    for l in lines:
        if runs and runs[-1][0]["code"] == l["code"]:
            runs[-1].append(l)
        else:
            runs.append([l])
    while len(runs) < max(1, lo // 2) and any(len(r) > 1 for r in runs):   # too few speaking shots: split the longest run
        i = max(range(len(runs)), key=lambda k: len(runs[k]))
        half = len(runs[i]) // 2
        runs[i:i + 1] = [runs[i][:half], runs[i][half:]]
    gaps = min(len(runs), max(0, hi - len(runs)))
    scene_of = {}
    cursor = 0
    for scene in episode["script"]["scenes"]:
        for _ in scene["lines"]:
            scene_of[cursor] = scene["index"]
            cursor += 1
    plan = wardrobe_for(sid, episode["number"])
    outfits = outfit_codes(sid)
    default_set = episode["script"]["scenes"][0]

    def outfit(code, scene):
        return plan.get((code, scene)) or sorted(outfits.get(code, {"W01"}))[0]

    setup = CameraSetupDef(name="Static medium", shot_size="medium", angle="eye level", movement="static", composition="subject centred", best_for="calm dialogue")
    shots: list[ShotSpec] = []
    for n, run in enumerate(runs):
        scene = scene_of[run[0]["idx"]]
        if n < gaps:
            shots.append(ShotSpec(idx=len(shots), shot_type="non_speaking", scene_index=scene, set_code=default_set["set_code"], time_of_day=default_set["time_of_day"],
                                  camera_setup_name=setup.name, new_setup=setup, characters=[ShotCharacter(code=run[0]["code"], outfit_code=outfit(run[0]["code"], scene), position="frame centre", mouth_visible_speaking=False)],
                                  line_indexes=[], action="A held look.", sounds=["room tone"], is_hook=not shots, is_cliffhanger=False, placement_kind=None, est_seconds=9.0 if shots else 3.5))
        shots.append(ShotSpec(idx=len(shots), shot_type="speaking_closeup" if n % 2 == 0 else "speaking_wider", scene_index=scene, set_code=default_set["set_code"],
                              time_of_day=default_set["time_of_day"], camera_setup_name=setup.name, new_setup=setup,
                              characters=[ShotCharacter(code=run[0]["code"], outfit_code=outfit(run[0]["code"], scene), position="frame centre", mouth_visible_speaking=True)],
                              line_indexes=[l["idx"] for l in run], action="A quiet exchange.", sounds=["room tone"], is_hook=not shots, is_cliffhanger=False, placement_kind=None, est_seconds=4.0))
    shots[0] = shots[0].model_copy(update={"is_hook": True})
    shots[-1] = shots[-1].model_copy(update={"is_cliffhanger": True})
    shots = _with_placements(sid, episode, shots, setup, hi)
    return ShotList(episode_number=episode["number"], shots=[s.model_copy(update={"idx": i}) for i, s in enumerate(shots)])


def _with_placements(sid, episode: dict, shots: list[ShotSpec], setup, hi: int) -> list[ShotSpec]:
    """Mark the shots that carry the planned brand placements: a non-speaking shot in the middle for a visual kind (so it can run long enough), the first
    speaking shot for a spoken mention."""
    from app import placement

    for p in episode_placements(sid, episode["number"]):
        kind = p["kind"]
        if kind == "spoken":
            i = next((i for i, s in enumerate(shots) if s.line_indexes), 0)
            shots[i] = shots[i].model_copy(update={"placement_kind": kind})
            continue
        i = next((i for i, s in enumerate(shots) if not s.line_indexes and 0 < i < len(shots) - 1), None)
        if i is None:   # no free non-speaking shot: use a wider speaking shot
            i = next(i for i, s in enumerate(shots) if s.shot_type == "speaking_wider")
        shots[i] = shots[i].model_copy(update={"placement_kind": kind, "est_seconds": max(shots[i].est_seconds, 6.0),
                                              "action": "She takes the product from her bag and uses it naturally while she talks." if kind == "product_in_use"
                                              else "She holds the product up with its label toward the camera." if kind == "product_shown" else shots[i].action})
    return shots


# ---------------------------------------------------------------- shot requests (llm_shot_request)

REQUEST_INSTRUCTIONS = """You write the text prompt for one video clip of a vertical micro-drama. Return a ShotRequest: prompt (at most 1,500 characters) and camera_fixed.

Prompt rules by clip type:
- Non-speaking clip: describe the action and the camera; list the sounds; end with "No music. No speech."
- Speaking wider shot or hero shot: describe the action; state "The character speaks the following line, mouth clearly moving, with this emotion"; include the line text and the emotion; end with "No other character speaks."
- Speaking close-up: ONE sentence of acting direction only, for example the emotion and a small gesture. No camera description.
Never include a character's name, a brand name, or on-screen text in a prompt. Refer to people as "the woman", "the man", "the older man" and so on.
camera_fixed is true only when the camera must not move at all.
Follow the camera setup text for the target model exactly where it gives one. Apply the learned rules."""

BAD_TEXT_WORDS = ("caption", "subtitle", "on-screen", "on screen", "title card", "lower third", "watermark")
MAX_PROMPT = 1500


def request_kind(tier: str, has_lines: bool, fallback_used: bool = False) -> str:
    from app.media.routing import method_tier

    tier = method_tier(tier, fallback_used, 1 if has_lines else 0)
    if tier == "B1":
        return "closeup"
    if has_lines and tier in ("B2", "C", "S"):
        return "speaking"
    return "silent"


def learned_rules(sid, model_key: str, query: str, limit: int = 5) -> tuple[list[dict], dict | None]:
    (vec,), usage = images.embed([query])
    rows = db.fetch_all(
        "select id, rule_text from learned_rules where active and model_key = %s and embedding is not null order by embedding <=> %s::vector limit %s",
        (model_key, db.to_vector(vec), limit))
    return rows, usage


def camera_text(shot: dict, model_key: str) -> str:
    cam = db.fetch_one("select * from camera_setups where id = %s", (shot["camera_setup_id"],)) if shot["camera_setup_id"] else None
    if not cam:
        return ""
    specific = (cam["prompt_by_model"] or {}).get(model_key)
    return specific or f"{cam['shot_size']} shot, {cam['angle']} angle, {cam['movement']} camera, {cam['composition']}"


def request_material(sid, shot: dict, model_key: str, rules: list[dict]) -> str:
    spec = shot["spec"]
    lines = []
    for lid in shot["line_ids"]:
        l = db.fetch_one("select text, emotion from dialogue_lines where id = %s", (lid,))
        lines.append(f'"{l["text"]}" (emotion: {l["emotion"]})')
    return "\n".join(x for x in [
        f"Target model: {model_key}. Clip type: {request_kind(shot['tier'], bool(lines), shot.get('fallback_used', False))} (tier {shot['tier']}).",
        f"Action: {spec['action']}",
        f"Sounds: {', '.join(spec.get('sounds') or []) or 'none'}",
        f"Camera setup: {camera_text(shot, model_key)}",
        ("Line(s) spoken:\n" + "\n".join(lines)) if lines else "",
        ("Rules learned from earlier failures:\n" + "\n".join(f"- {r['rule_text']}" for r in rules)) if rules else "",
        feedback_line(spec),
    ] if x)


def feedback_line(spec: dict) -> str:
    """A reviewer's note on this shot from the finished episode (Gate 4 or 5 feedback): the writer applies it when it rewrites the shot's request."""
    note = (spec or {}).get("feedback_note")
    return f"Reviewer feedback on the previous version of this shot (apply it): {note}" if note else ""


def fixture_request(shot: dict, model_key: str) -> "ShotRequest":
    from app.models.shots import ShotRequest

    spec = shot["spec"]
    kind = request_kind(shot["tier"], bool(shot["line_ids"]), shot.get("fallback_used", False))
    if kind == "closeup":
        return ShotRequest(prompt="She holds the other person's gaze, jaw tight, and breathes in before she speaks.", camera_fixed=True)
    if kind == "speaking":
        rows = [db.fetch_one("select text, emotion from dialogue_lines where id = %s", (i,)) for i in shot["line_ids"]]
        said = " ".join(f'"{l["text"]}"' for l in rows)
        return ShotRequest(prompt=f'{spec["action"]} The character speaks the following line, mouth clearly moving, with this emotion: {rows[0]["emotion"]}. {said} No other character speaks.', camera_fixed=False)
    sounds = ", ".join(spec.get("sounds") or []) or "room tone"
    return ShotRequest(prompt=f"{spec['action']} Slow, steady camera. Sounds: {sounds}. No music. No speech.", camera_fixed=False)


def validate_request(sid, shot: dict, req) -> list[str]:
    problems: list[str] = []
    text = req.prompt.strip()
    if not text:
        return ["the prompt is empty"]
    if len(text) > MAX_PROMPT:
        problems.append(f"the prompt is {len(text)} characters; the limit is {MAX_PROMPT}")
    kind = request_kind(shot["tier"], bool(shot["line_ids"]), shot.get("fallback_used", False))
    # spoken line text may contain names, so check the prompt with the quoted line text removed
    check = text
    for lid in shot["line_ids"]:
        line = db.fetch_one("select text from dialogue_lines where id = %s", (lid,))["text"]
        check = check.replace(line, " ")
    low = check.lower()
    for c in db.fetch_all("select code, name from characters where series_id = %s", (sid,)):
        if c["name"] and c["name"].lower() in low:
            problems.append(f"the prompt names the character {c['name']}; describe them instead")
        if c["code"].lower() in low:
            problems.append(f"the prompt contains the character code {c['code']}")
    brief = db.fetch_one("select brand_name from brand_briefs where series_id = %s order by version desc limit 1", (sid,))
    if brief and brief["brand_name"] and brief["brand_name"].lower() in low:
        problems.append("the prompt names the brand")
    if any(w in low for w in BAD_TEXT_WORDS):
        problems.append("the prompt asks for on-screen text")
    if kind == "silent" and not text.endswith("No music. No speech."):
        problems.append('a non-speaking prompt must end with "No music. No speech."')
    if kind == "speaking":
        if "The character speaks the following line, mouth clearly moving, with this emotion" not in text:
            problems.append('a speaking prompt must contain "The character speaks the following line, mouth clearly moving, with this emotion"')
        if not text.endswith("No other character speaks."):
            problems.append('a speaking prompt must end with "No other character speaks."')
        for lid in shot["line_ids"]:
            if db.fetch_one("select text from dialogue_lines where id = %s", (lid,))["text"] not in text:
                problems.append("the prompt must include the line text")
                break
    if kind == "closeup":
        sentences = [s for s in text.replace("!", ".").replace("?", ".").split(".") if s.strip()]
        if len(sentences) != 1:
            problems.append(f"a close-up prompt is one sentence of acting direction, not {len(sentences)}")
    return problems


# ---------------------------------------------------------------- brand shot requests: one validated action sentence

MAX_ACTION = 300


def validate_brand_action(sid, text: str) -> list[str]:
    """The compiler's one sentence for a brand shot. Code adds the product lock, motion guard and @Image tags, so this text must carry none of them."""
    from app.text import contains_term, normalize_for_match

    problems: list[str] = []
    text = (text or "").strip()
    if not text:
        return ["the action sentence is empty"]
    if len(text) > MAX_ACTION:
        problems.append(f"the action is {len(text)} characters; the limit is {MAX_ACTION}")
    sentences = [x for x in text.replace("!", ".").replace("?", ".").split(".") if x.strip()]
    if len(sentences) != 1:
        problems.append(f"write exactly one sentence, not {len(sentences)}")
    if "@" in text or "image" in text.lower() and "reference" in text.lower():
        problems.append("do not refer to reference images or use @ tags; code adds them")
    low = text.lower()
    norm = normalize_for_match(text)
    for c in db.fetch_all("select code, name from characters where series_id = %s", (sid,)):
        if c["name"] and c["name"].lower() in low:
            problems.append(f"the action names the character {c['name']}")
        if c["code"].lower() in low:
            problems.append(f"the action contains the character code {c['code']}")
    brief = db.fetch_one("select brand_name, product, label_texts from brand_briefs where series_id = %s order by version desc limit 1", (sid,))
    if brief:
        for term in [brief["brand_name"], brief["product"], *(brief["label_texts"] or [])]:
            t = normalize_for_match(term)
            if len(t) >= 3 and contains_term(norm, t):
                problems.append(f"the action names the brand, the product or printed text ({term!r}); code adds the product description")
    if any(w in low for w in BAD_TEXT_WORDS):
        problems.append("the action asks for on-screen text")
    return problems
