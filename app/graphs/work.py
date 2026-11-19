"""Work items for each graph step. Every builder selects work by status, so running a node twice
enqueues nothing new (guide 8.2). Builders read and write Postgres; they never call a vendor."""
from __future__ import annotations

import hashlib

from app import db, library
from app.config import get_config
from app.graphs import common
from app.graphs.steps import WorkItem
from app.media import costs
from app.media.routing import is_brand, lipsync_engine, method_tier, needs_lipsync, shot_model_key
from app.models.enums import JobKind, Vendor
from app.stages import look
from app.vendors import eleven, falmedia, images

ANTHROPIC, OPENAI, ELEVEN, FAL, LOCAL = Vendor.anthropic, Vendor.openai, Vendor.elevenlabs, Vendor.fal, Vendor.local


def _fb(shot: dict) -> dict:
    """The feedback item that sent this shot back for rework goes into every job request, so the ledger key is new (guide 12.6)."""
    fid = (shot.get("spec") or {}).get("feedback_id")
    return {"fb": fid} if fid else {}


def _digest(parts) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:12]


# ------------------------------------------------------------------ story

def story_ingest_items(sid, batch, rnd) -> list[WorkItem]:
    series = common.get_series(sid)
    if series["genre_id"] is not None:  # library genre: nothing to ingest
        return []
    brief = common.latest_brief(sid)
    source_id = library.ensure_story_source(sid, brief["story_text"] or "")
    return [
        WorkItem(JobKind.ingest_source, ANTHROPIC, {"source_id": str(source_id)}),
        WorkItem(JobKind.llm_story_check, ANTHROPIC, {"brief_version": brief["version"], "source_id": str(source_id)}),
    ]


def bible_items(sid, batch, rnd) -> list[WorkItem]:
    return [WorkItem(JobKind.llm_bible, ANTHROPIC, {"rev": common.gate_revs(sid, 2), "notes": common.latest_note(sid, "reviewer_bible")})]


def beats_items(sid, batch, rnd) -> list[WorkItem]:
    # "try" counts validation failures: a redo must be a different request, or the ledger would hand back the old answer
    return [WorkItem(JobKind.llm_beats, ANTHROPIC, {
        "rev": common.gate_revs(sid, 2), "notes": common.latest_note(sid, "reviewer_beats"), "errors": common.latest_note(sid, "beats_errors"),
        "try": common.count_notes(sid, "beats_redo")})]


def placement_items(sid, batch, rnd) -> list[WorkItem]:
    if common.get_series(sid)["category"] != "ad_placement":
        return []
    return [WorkItem(JobKind.llm_placement_plan, ANTHROPIC, {"rev": common.gate_revs(sid, 2), "try": common.count_notes(sid, "beats_redo")})]


def scripts_items(sid, batch, rnd) -> list[WorkItem]:
    notes = common.latest_note(sid, "reviewer_scripts")
    rev = common.gate_revs(sid, 2)
    return [
        WorkItem(JobKind.llm_script, ANTHROPIC, {"episode": e["number"], "rev": rev, "notes": notes}, episode_id=e["id"])
        for e in db.fetch_all("select id, number from episodes where series_id = %s and status = 'outlined' order by number", (sid,))
    ]


def continuity_items(sid, batch, rnd) -> list[WorkItem]:
    return [WorkItem(JobKind.llm_continuity, ANTHROPIC, {"rev": common.gate_revs(sid, 2)})]


def story_check_items(sid, batch, rnd) -> list[WorkItem]:
    rev = common.gate_revs(sid, 2)
    return [WorkItem(JobKind.llm_wardrobe, ANTHROPIC, {"rev": rev}), WorkItem(JobKind.llm_originality, ANTHROPIC, {"rev": rev})]


# ------------------------------------------------------------------ look

def traits_items(sid, batch, rnd) -> list[WorkItem]:
    items = []
    for ref in db.fetch_all("select id from brief_refs where series_id = %s and kind = 'character' order by created_at", (sid,)):
        items.append(WorkItem(JobKind.llm_trait_sheet, ANTHROPIC, {"ref_id": str(ref["id"])}))
    speaking = common.speaking_codes(sid)
    for c in db.fetch_all("select id, code from characters where series_id = %s order by code", (sid,)):
        if c["code"] in speaking:
            items.append(WorkItem(JobKind.llm_voice_profile, ANTHROPIC, {"character_id": str(c["id"])}))
    return items


def masters_items(sid, batch, rnd) -> list[WorkItem]:
    """image_master per character without a master. A regeneration after a likeness failure is try 2
    (with the photo) or try 3 (without it)."""
    items = []
    for c in db.fetch_all(
        "select c.id, c.code from characters c where c.series_id = %s and not exists"
        " (select 1 from character_assets a where a.character_id = c.id and a.kind = 'master') order by c.code", (sid,)
    ):
        n = 1 + common.count_notes(sid, f"likeness_retry:{c['id']}")
        items.append(WorkItem(JobKind.image_master, OPENAI, {"character_id": str(c["id"]), "try": n, "use_photo": n <= 2}))
    return items


def emotion_group_items(sid, batch, rnd) -> list[WorkItem]:
    speaking = common.speaking_codes(sid)
    return [
        WorkItem(JobKind.llm_audio_tag, OPENAI, {"purpose": "emotion_groups", "character_id": str(c["id"])})
        for c in db.fetch_all("select id, code from characters where series_id = %s and emotion_groups is null order by code", (sid,))
        if c["code"] in speaking
    ]


def pack_items(sid, batch, rnd) -> list[WorkItem]:
    rev, retry = common.gate_revs(sid, 3), common.count_notes(sid, "pack_retry")
    items: list[WorkItem] = []
    skip = images.unsupported_pack_kinds()   # sheets the image backend cannot make are skipped, visibly (Gate 3 page)
    chars = db.fetch_all("select * from characters where series_id = %s order by code", (sid,))
    for c in chars:
        outfits = [w["code"] for w in db.fetch_all("select code from wardrobe_items where character_id = %s order by code", (c["id"],))]
        groups = sorted(set((c["emotion_groups"] or {}).values()))
        have = {(a["kind"], _tag_key(a["tags"])) for a in db.fetch_all("select kind, tags from character_assets where character_id = %s", (c["id"],))}
        for spec in look.pack_plan(c["is_lead"], outfits, groups):
            if spec["kind"] not in skip and (spec["kind"], _tag_key(spec["tags"])) not in have:
                items.append(WorkItem(JobKind.image_pack, OPENAI, {"character_id": str(c["id"]), "kind": spec["kind"], "tags": spec["tags"], "try": retry, "rev": rev}))
    if chars and "lineup" not in skip and not db.fetch_one("select 1 from character_assets where series_id = %s and kind = 'lineup'", (sid,)):
        items.append(WorkItem(JobKind.image_pack, OPENAI, {"character_id": None, "kind": "lineup", "tags": {}, "try": retry, "rev": rev}))
    for s in db.fetch_all("select id, code, time_of_day from sets where series_id = %s and plate_r2_key is null order by code, time_of_day", (sid,)):
        items.append(WorkItem(JobKind.image_set_plate, OPENAI, {"set_id": str(s["id"]), "rev": rev}))
    for s in db.fetch_all("select distinct code from sets where series_id = %s and ambience_r2_key is null order by code", (sid,)):
        items.append(WorkItem(JobKind.sfx_ambience, ELEVEN, {"set_code": s["code"], "rev": rev}, estimated_cost=eleven.sfx_cost(30)))
    if db.fetch_one("select 1 from brand_briefs where series_id = %s and product_sheet is null limit 1", (sid,)):   # ad placement: describe the real product once
        items.append(WorkItem(JobKind.llm_product_sheet, ANTHROPIC, {"try": retry, "rev": rev}))
    speaking = common.speaking_codes(sid)
    for c in chars:
        if c["code"] in speaking and not db.fetch_one("select 1 from character_assets where character_id = %s and kind = 'voice_sample'", (c["id"],)):
            items.append(WorkItem(JobKind.voice_design, ELEVEN, {"character_id": str(c["id"]), "try": retry, "rev": rev}))
    return items


def _tag_key(tags: dict) -> str:
    return "|".join(f"{k}={tags[k]}" for k in sorted(tags))


# ------------------------------------------------------------------ build

def shot_list_items(sid, batch, rnd) -> list[WorkItem]:
    rev = common.gate_revs(sid, 2)
    return [
        WorkItem(JobKind.llm_shot_list, ANTHROPIC, {"episode": e["number"], "rev": rev, "try": common.count_notes(sid, f"shots_redo:{e['number']}"),
                                                    "errors": common.latest_note(sid, f"shots_errors:{e['number']}"), "standing": common.latest_note(sid, "standing_notes")}, episode_id=e["id"])
        for e in common.batch_episodes(sid, batch, ("scripted",))
    ]


def audio_tag_items(sid, batch, rnd) -> list[WorkItem]:
    items = []
    for e in common.batch_episodes(sid, batch):
        lines = db.fetch_all("select id, version from dialogue_lines where episode_id = %s and audio_tag is null order by idx", (e["id"],))
        if lines:
            items.append(WorkItem(JobKind.llm_audio_tag, OPENAI, {"purpose": "line_tags", "episode": e["number"], "lines": _digest(f"{l['id']}:{l['version']}" for l in lines),
                                                                                  **({"note": note} if (note := common.latest_note(sid, f"voice_note:{e['number']}")) else {})}, episode_id=e["id"]))
    return items


def tts_items(sid, batch, rnd) -> list[WorkItem]:
    items = []
    price = get_config("price_book")["speech_per_1k_chars"]
    for e in common.batch_episodes(sid, batch):
        for l in db.fetch_all("select id, version, text, character_id from dialogue_lines where episode_id = %s and audio_r2_key is null order by idx", (e["id"],)):
            items.append(WorkItem(JobKind.tts_line, ELEVEN, {"line_id": str(l["id"]), "version": l["version"]}, episode_id=e["id"], estimated_cost=len(l["text"]) / 1000 * price))
    return items


def keyframe_cache_key(spec: dict) -> str:
    """character, outfit, emotion group, set, time of day, camera setup (guide 9.6)."""
    chars = ",".join(f"{c['code']}:{c['outfit_code']}" for c in spec.get("characters", []))
    brand = f"brand:{spec.get('placement_kind')}:{spec.get('idx')}" if spec.get("brand") else ""   # a brand shot never shares a keyframe with another shot
    return "|".join([brand, chars, str(spec.get("emotion_group", "")), spec.get("set_code", ""), spec.get("time_of_day", ""), spec.get("camera_setup_name", "")])


def keyframe_items(sid, batch, rnd) -> list[WorkItem]:
    """image_keyframe per planned shot, after a cache lookup: a shot whose cache key already has a
    keyframe takes it without a job; one job per distinct key; repaired shots (attempts > 0) never use the cache."""
    shots = db.fetch_all(
        "select s.*, e.number episode_number from shots s join episodes e on e.id = s.episode_id"
        " where s.series_id = %s and e.batch = %s and s.status = 'planned' and s.tier <> 'INSERT' order by e.number, s.idx", (sid, batch),
    )
    done = {}
    for s in db.fetch_all("select spec, keyframe_r2_key from shots where series_id = %s and keyframe_r2_key is not null and attempts = 0", (sid,)):
        done.setdefault(keyframe_cache_key(s["spec"]), s["keyframe_r2_key"])
    items, queued_keys = [], set()
    for s in shots:
        key = keyframe_cache_key(s["spec"])
        if s["attempts"] == 0 and not _fb(s):   # a repaired shot or one sent back by feedback is always drawn again
            if key in done:
                db.execute("update shots set keyframe_r2_key = %s, status = 'keyframe_ready' where id = %s", (done[key], s["id"]))
                continue
            if key in queued_keys:
                continue  # the representative's job will link this shot when it finishes
            queued_keys.add(key)
        items.append(WorkItem(JobKind.image_keyframe, OPENAI, {"shot_id": str(s["id"]), "attempt": s["attempts"], "cache_key": key, **_fb(s)}, episode_id=s["episode_id"], shot_id=s["id"]))
    return items


def shot_request_items(sid, batch, rnd) -> list[WorkItem]:
    return [
        WorkItem(JobKind.llm_shot_request, ANTHROPIC, {"shot_id": str(s["id"]), "attempts": s["attempts"], "model_key": shot_model_key(s), **_fb(s)},
                 episode_id=s["episode_id"], shot_id=s["id"])
        for s in _batch_shots(sid, batch, ("keyframe_ready",)) if s["tier"] != "INSERT"
    ]


_batch_shots = common.batch_shots


def planned_video_cost(sid, batch) -> float:
    """The batch budget: every clip and lip-sync step, one attempt each (guide 7.5)."""
    total = 0.0
    for s in db.fetch_all(
        "select s.tier, s.fallback_used, s.spec, s.planned_duration, s.render_duration, cardinality(s.line_ids) line_count from shots s join episodes e on e.id = s.episode_id"
        " where s.series_id = %s and e.batch = %s", (sid, batch),
    ):
        if s["planned_duration"] is None:
            continue
        total += costs.shot_clip_cost(s["tier"], float(s["planned_duration"]), s["render_duration"], s["line_count"], s["fallback_used"], is_brand(s["spec"]))
        total += costs.shot_lipsync_cost(s["tier"], s["line_count"], s["render_duration"], s["fallback_used"])
    return round(total, 6)


def clip_items(sid, batch, rnd) -> list[WorkItem]:
    from app.media import requests as media_requests

    items = []
    for s in _batch_shots(sid, batch, ("keyframe_ready", "rendering")):
        if s["tier"] == "INSERT":
            items.append(WorkItem(JobKind.ffmpeg_insert, LOCAL, {"shot_id": str(s["id"]), "attempt": s["attempts"], **_fb(s)}, episode_id=s["episode_id"], shot_id=s["id"]))
            continue
        prompt = _latest_prompt(s["id"], s["attempts"])
        audio_key = f"series/{sid}/episodes/{s['episode_number']:02d}/shots/{s['idx']}/shot_audio.mp3"
        if is_brand(s["spec"]) and s["tier"] in ("A", "B2"):
            items.append(_brand_clip_item(sid, s, prompt, audio_key))
            continue
        mt = method_tier(s["tier"], s["fallback_used"], s["line_count"])
        req = media_requests.video_request(mt, {"id": str(s["id"]), "keyframe_r2_key": s["keyframe_r2_key"], "render_duration": s["render_duration"],
                                                        "attempts": s["attempts"], "line_count": s["line_count"]}, prompt["prompt"], prompt["camera_fixed"], audio_key)
        cost = costs.shot_clip_cost(s["tier"], float(s["planned_duration"]), s["render_duration"], s["line_count"], s["fallback_used"], is_brand(s["spec"]))
        items.append(WorkItem(JobKind.video_clip, FAL, {"tier": mt, "shot_id": str(s["id"]), "attempt": s["attempts"], "endpoint": req["endpoint"], "arguments": req["arguments"], "shot_audio_key": audio_key, **_fb(s)},
                              endpoint=req["endpoint"], episode_id=s["episode_id"], shot_id=s["id"], budget_class="video", estimated_cost=cost))
    return items


def _brand_clip_item(sid, s, prompt: dict, audio_key: str) -> WorkItem:
    """A brand shot goes to Seedance 2.0 reference-to-video with its keyframe and the REAL product photos and logo. The final prompt is assembled by code
    from fixed templates and the one validated action sentence (app/stages/brand.py)."""
    from app import placement
    from app.media import requests as media_requests
    from app.stages import brand

    kind = s["spec"]["placement_kind"]
    rows = placement.reference_assets(sid, kind)
    products = [r["r2_key"] for r in rows if r["kind"] != "logo"]
    logo = next((r["r2_key"] for r in rows if r["kind"] == "logo"), None)
    keys = [s["keyframe_r2_key"], *products, *([logo] if logo else [])]
    tag = brand.tags(1, len(products), bool(logo))
    line = " ".join(f'"{l["text"]}"' for l in db.fetch_all("select text from dialogue_lines where id = any(%s) order by idx", (s["line_ids"],))) if s["line_ids"] else None
    minimum = float((placement.brief(sid) or {}).get("min_exposure_seconds") or 3)
    text = brand.compose_video_prompt(prompt["prompt"], kind, tag, int(minimum), s["attempts"], bool(s["line_count"]), line)
    req = media_requests.brand_video_request(keys, text, s["render_duration"], audio=(s["tier"] == "A"))
    cost = costs.shot_clip_cost(s["tier"], float(s["planned_duration"]), s["render_duration"], s["line_count"], s["fallback_used"], True)
    return WorkItem(JobKind.video_clip, FAL, {"tier": s["tier"], "shot_id": str(s["id"]), "attempt": s["attempts"], "endpoint": req["endpoint"], "arguments": req["arguments"],
                                              "shot_audio_key": audio_key, "brand": kind, **_fb(s)},
                    endpoint=req["endpoint"], episode_id=s["episode_id"], shot_id=s["id"], budget_class="video", estimated_cost=cost)


def _latest_prompt(shot_id, attempts) -> dict:
    row = db.fetch_one(
        "select result from jobs where kind = 'llm_shot_request' and shot_id = %s and status = 'succeeded' order by finished_at desc limit 1", (shot_id,)
    )
    result = (row or {}).get("result") or {}
    return {"prompt": result.get("prompt", ""), "camera_fixed": bool(result.get("camera_fixed", False))}


def lipsync_items(sid, batch, rnd) -> list[WorkItem]:
    from app.media import requests as media_requests

    items = []
    for s in _batch_shots(sid, batch, ("rendered", "syncing")):
        if not needs_lipsync(s["tier"], s["line_count"], s["fallback_used"]):
            continue
        audio_key = f"series/{sid}/episodes/{s['episode_number']:02d}/shots/{s['idx']}/shot_audio.mp3"
        kling_failed = db.fetch_one("select count(*) n from jobs where kind = 'lipsync' and shot_id = %s and endpoint = %s and status = 'failed'", (s["id"], falmedia.KLING_LIPSYNC))["n"]
        engine = lipsync_engine(s["sync_attempts"], kling_failed, str(get_config("lipsync_fallback")))
        req = media_requests.lipsync_request(s["raw_clip_r2_key"], audio_key, engine, seed_from=f"{s['id']}{s['sync_attempts']}")
        cost = costs.shot_lipsync_cost(s["tier"], s["line_count"], s["render_duration"], s["fallback_used"], engine)
        items.append(WorkItem(JobKind.lipsync, FAL, {"shot_id": str(s["id"]), "sync_attempt": s["sync_attempts"], "endpoint": req["endpoint"], "arguments": req["arguments"], **_fb(s)},
                              endpoint=req["endpoint"], episode_id=s["episode_id"], shot_id=s["id"], budget_class="video", estimated_cost=cost))
    return items


def sound_items(sid, batch, rnd) -> list[WorkItem]:
    items = []
    for e in common.batch_episodes(sid, batch):
        if e["music_r2_key"] is None:
            ms = int((float(e["runtime_seconds"] or 80)) * 1000)
            items.append(WorkItem(JobKind.music_episode, ELEVEN, {"episode": e["number"], "length_ms": ms, "note": common.latest_note(sid, f"music_note:{e['number']}")},
                                  episode_id=e["id"], estimated_cost=eleven.music_cost(ms)))
    return items


def compile_items(sid, batch, rnd) -> list[WorkItem]:
    items = []
    for e in common.batch_episodes(sid, batch, ("rendered",)):
        bad = db.fetch_one("select 1 from shots where episode_id = %s and status not in ('ready','qa_passed') limit 1", (e["id"],))
        if not bad:
            items.append(WorkItem(JobKind.ffmpeg_compile, LOCAL, {"episode": e["number"], "cut": e["cut_version"] + 1}, episode_id=e["id"]))
    return items


def qa_items(sid, batch, rnd) -> list[WorkItem]:
    qa_round = "qa1" if batch == "pilot" else "qa2"
    items = []
    for e in common.batch_episodes(sid, batch, ("compiled",)):
        for s in db.fetch_all("select id, attempts, sync_attempts from shots where episode_id = %s and status = 'ready' order by idx", (e["id"],)):
            items.append(WorkItem(JobKind.qa_shot, LOCAL, {"shot_id": str(s["id"]), "attempts": s["attempts"], "sync": s["sync_attempts"], "cut": e["cut_version"], "round": qa_round},
                                  episode_id=e["id"], shot_id=s["id"]))
        items.append(WorkItem(JobKind.qa_episode, LOCAL, {"episode": e["number"], "cut": e["cut_version"], "round": qa_round}, episode_id=e["id"]))
        if db.fetch_one("select 1 from placements where series_id = %s and episode_number = %s limit 1", (sid, e["number"])):
            items.append(WorkItem(JobKind.qa_brand, ANTHROPIC, {"episode": e["number"], "cut": e["cut_version"], "round": qa_round}, episode_id=e["id"]))
    return items


def learn_items(sid, batch, rnd) -> list[WorkItem]:
    return [
        WorkItem(JobKind.llm_rule, ANTHROPIC, {"shot_id": str(r["shot_id"]), "check": r["check_name"], "round": rnd}, shot_id=r["shot_id"])
        for r in db.fetch_all(
            "select distinct q.shot_id, q.check_name from qa_results q join shots s on s.id = q.shot_id join episodes e on e.id = s.episode_id"
            " where q.series_id = %s and e.batch = %s and q.passed = false and q.check_name in ('vision','lipsync') and q.category is distinct from 'unscored'", (sid, batch),
        )
    ]


def delivery_items(sid, batch, rnd) -> list[WorkItem]:
    items = [WorkItem(JobKind.ffmpeg_master, LOCAL, {"rev": common.gate_revs(sid, 5)})]
    items += [WorkItem(JobKind.llm_marketing, OPENAI, {"episode": e["number"]}, episode_id=e["id"]) for e in db.fetch_all("select id, number from episodes where series_id = %s order by number", (sid,))]
    return items
