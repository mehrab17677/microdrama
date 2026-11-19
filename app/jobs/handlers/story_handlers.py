"""Real handlers for the story stages (guide 9.4): bible, beats, placement plan, scripts, continuity,
wardrobe, originality. Roles: story (bible, beats), writer (the rest), originality (plot comparison).

Each handler gathers context, asks the model (or a dry-run fixture) for a schema object, validates it
in code and raises PermanentError with every problem; the graph retries as a new attempt and the next
attempt is shown what went wrong. Persistence happens only after validation passes.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from app import db
from app.jobs.errors import PermanentError
from app.models.story import (BeatSheetOut, ContinuityReport, EpisodeBeat, EpisodeScript, PlacementPlan, StoryBible, WardrobePlan)
from app.stages import brand as brand_stage, originality, story, story_fixtures as fx
from app.stages.ingest import _cost_rows
from app.vendors import llm

CHUNK_ATTEMPTS = 3   # tries per beat-sheet chunk before the whole job fails
KINDS = ["llm_bible", "llm_beats", "llm_placement_plan", "llm_script", "llm_continuity", "llm_wardrobe", "llm_originality"]


def _fail_if(problems: list[str]) -> None:
    if problems:
        raise PermanentError(f"{len(problems)} problem(s): " + "; ".join(problems[:12]))


def _result(usages: list[dict], **extra) -> dict:
    usages = [u for u in usages if u]
    return {**extra, "actual_cost": round(sum(u["cost_usd"] for u in usages), 6), "cost_log": _cost_rows(usages)}


def _user(material: str, job: dict) -> list[dict]:
    req = job["request"]
    notes = "\n".join(x for x in (req.get("notes", ""), ("Problems found in the last version of this step:\n" + req["errors"]) if req.get("errors") else "") if x)
    return [{"role": "user", "content": material + story.notes_block(job, notes)}]


# ---------------------------------------------------------------- bible

def llm_bible(job: dict) -> dict:
    sid = job["series_id"]
    ctx = story.series_context(sid)
    cards, embed_usage = story.retrieve_cards(ctx)
    brief = ctx["brief"]
    material = "\n\n".join([
        f"Genre: {ctx['genre_name']}",
        "Keywords: " + ", ".join(brief["keywords"]),
        ("Aesthetic note: " + brief["aesthetic_note"]) if brief["aesthetic_note"] else "",
        f"Intake role labels (each becomes one fixed character slot): {json.dumps(ctx['roles'], ensure_ascii=False)}",
        ("Brand brief (keep the story natural; the product appears only through the planned placements, as a normal part of a scene):\n" + brand_stage.data_block(ctx["brand"])
         + "\n" + json.dumps({k: ctx["brand"][k] for k in ("brand_name", "placement_types", "frequency_note", "exclusions", "forbidden_claims")}, ensure_ascii=False)) if ctx["brand"] else "",
        "Pattern cards (structure only, never names or plot):\n" + "\n".join(f"- [{c['kind']}] {c['title']}: {c['body']}" for c in cards) if cards else "No reference pattern cards are available.",
    ])
    bible, usage = llm.complete("story", story.build_system(story.BIBLE_INSTRUCTIONS, ctx, with_bible=False), _user(material, job), schema=StoryBible,
                                fixture=lambda: fx.make_bible(ctx))
    _fail_if(story.validate_bible(bible, ctx["roles"], story.blocklist(sid)))
    story.persist_bible(sid, bible)
    return _result([embed_usage, usage], episodes=bible.episode_count, characters=len(bible.characters))


# ---------------------------------------------------------------- beats

def llm_beats(job: dict) -> dict:
    sid = job["series_id"]
    ctx = story.series_context(sid)
    bible = ctx["bible"]
    if bible is None:
        raise PermanentError("there is no Story Bible yet")
    terms = story.blocklist(sid)
    cards, embed_usage = story.retrieve_cards(ctx)
    cards_text = "\n".join(f"- [{c['kind']}] {c['title']}: {c['body']}" for c in cards) or "No reference pattern cards are available."
    beats: list[EpisodeBeat] = []
    usages = [embed_usage]
    cursor = 1
    while cursor <= bible.episode_count:
        end = min(bible.episode_count, cursor + story.BEAT_CHUNK - 1)
        feedback = ""
        for attempt in range(CHUNK_ATTEMPTS):
            material = f"Write episodes {cursor} to {end} of {bible.episode_count}.\n\nPattern cards (structure only):\n{cards_text}"
            if beats:
                material += "\n\nThe episodes so far (continue from the last handoff):\n" + "\n".join(f"{b.number}. {b.summary} | handoff: {json.dumps(b.handoff, ensure_ascii=False)}" for b in beats[-4:])
            out, usage = llm.complete("story", story.build_system(story.BEATS_INSTRUCTIONS, ctx), _user(material + feedback, job), schema=BeatSheetOut,
                                      fixture=lambda s=cursor, e=end: BeatSheetOut.from_sheet(fx.make_beats(ctx, s, e)), cache_system=True)
            usages.append(usage)
            chunk = out.to_sheet().episodes
            problems = story.validate_beat_chunk(chunk, cursor, end, bible, terms)
            if not problems:
                beats += chunk
                cursor = end + 1
                break
            # A model that stops early still gave valid episodes: keep the good start and ask only for the rest.
            good = story.valid_prefix(chunk, cursor, bible, terms)
            if good:
                beats += good
                cursor += len(good)
                break
            feedback = "\n\nYour last answer for these episodes was rejected. Fix all of these:\n- " + "\n- ".join(problems[:8])
        else:
            raise PermanentError(f"episodes {cursor} to {end} failed validation {CHUNK_ATTEMPTS} times: " + "; ".join(problems[:6]))
    story.persist_beats(sid, beats)
    return _result(usages, episodes=len(beats))


# ---------------------------------------------------------------- placement plan (ad placement dramas)

def llm_placement_plan(job: dict) -> dict:
    sid = job["series_id"]
    ctx = story.series_context(sid)
    if not ctx["brand"]:
        return {"skipped": "no brand brief"}
    beats = {r["number"]: EpisodeBeat.model_validate(r["beat"]) for r in db.fetch_all("select number, beat from episodes where series_id = %s order by number", (sid,))}
    brand = ctx["brand"]
    material = ("Brand brief:\n" + brand_stage.data_block(brand) + "\n"
                + json.dumps({k: brand[k] for k in ("brand_name", "placement_types", "frequency_note", "exclusions", "spoken_wording", "forbidden_claims")}, ensure_ascii=False)
                + "\n\nBeat sheet:\n" + "\n".join(f"{n}. {b.summary}" for n, b in sorted(beats.items())))
    plan, usage = llm.complete("writer", story.build_system(story.PLACEMENT_INSTRUCTIONS, ctx), _user(material, job), schema=PlacementPlan,
                               fixture=lambda: fx.make_placement(ctx, list(beats)), cache_system=True)
    _fail_if(story.validate_placement(plan, brand, {n: b.summary for n, b in beats.items()}))
    story.persist_placement(sid, plan)
    return _result([usage], placements=len(plan.placements))


# ---------------------------------------------------------------- scripts

def _beat_neighbours(sid, number) -> tuple[EpisodeBeat | None, EpisodeBeat, EpisodeBeat | None]:
    def get(n):
        row = db.fetch_one("select beat from episodes where series_id = %s and number = %s", (sid, n))
        return EpisodeBeat.model_validate(row["beat"]) if row else None
    return get(number - 1), get(number), get(number + 1)


def generate_script(ctx: dict, number: int, job: dict, extra_notes: str = "") -> tuple[EpisodeScript, dict]:
    """One validated episode script (the model call plus the code checks). Raises PermanentError."""
    prev, beat, nxt = _beat_neighbours(ctx["id"], number)
    material = f"Write the script for episode {number}.\n\nThis episode's beat:\n{json.dumps(beat.model_dump(), ensure_ascii=False)}"
    if prev:
        material += f"\n\nPrevious episode (do not contradict its handoff): {prev.summary} | handoff: {json.dumps(prev.handoff, ensure_ascii=False)}"
    if nxt:
        material += f"\n\nNext episode (do not spoil it): {nxt.summary}"
    messages = [{"role": "user", "content": material + story.notes_block(job, "\n".join(x for x in (job["request"].get("notes", ""), extra_notes) if x))}]
    script, usage = llm.complete("writer", story.build_system(story.SCRIPT_INSTRUCTIONS, ctx), messages, schema=EpisodeScript,
                                 fixture=lambda: fx.make_script(ctx, number), cache_system=True)
    _fail_if(story.validate_script(script, number, ctx["bible"], ctx["language"]))
    return script, usage


def llm_script(job: dict) -> dict:
    sid = job["series_id"]
    ctx = story.series_context(sid)
    ep = db.fetch_one("select id, number from episodes where id = %s", (job["episode_id"],))
    script, usage = generate_script(ctx, ep["number"], job)
    story.persist_script(sid, ep["id"], script)
    return _result([usage], episode=ep["number"])


# ---------------------------------------------------------------- continuity and wardrobe

def _scripts_digest(scripts: dict) -> str:
    out = []
    for ep, sc in sorted(scripts.items()):
        out.append(f"## Episode {ep}\nHook: {sc.hook_action}")
        for scene in sc.scenes:
            out.append(f"Scene {scene.index} ({scene.set_code}, {scene.time_of_day}): {scene.action}")
            out += [f"  {i}. {l.character_code} [{l.emotion}]: {l.text}" for i, l in enumerate(scene.lines)]
        out.append(f"Cliffhanger: {sc.cliffhanger_action}")
    return "\n".join(out)


def llm_continuity(job: dict) -> dict:
    sid = job["series_id"]
    ctx = story.series_context(sid)
    scripts = story.load_scripts(sid)
    if not scripts:
        return {"edits": 0}
    report, usage = llm.complete("writer", story.build_system(story.CONTINUITY_INSTRUCTIONS, ctx), _user(_scripts_digest(scripts), job), schema=ContinuityReport,
                                 fixture=fx.make_continuity, cache_system=True)
    valid, skipped = story.validate_continuity(report, scripts, ctx["language"])
    applied = story.apply_continuity_edits(sid, valid, scripts)
    return _result([usage], edits=applied, skipped=skipped)


def llm_wardrobe(job: dict) -> dict:
    sid = job["series_id"]
    ctx = story.series_context(sid)
    scripts = story.load_scripts(sid)
    material = ("Characters:\n" + "\n".join(f"{c.code} {c.name} ({'lead' if c.is_lead else 'supporting'}): {c.visual_description}" for c in ctx["bible"].characters)
                + "\n\nWhere each character speaks (episode, scene): " + json.dumps({k: sorted(v) for k, v in story.line_appearances(scripts).items()})
                + "\n\nScripts:\n" + _scripts_digest(scripts))
    plan, usage = llm.complete("writer", story.build_system(story.WARDROBE_INSTRUCTIONS, ctx), _user(material, job), schema=WardrobePlan,
                               fixture=lambda: fx.make_wardrobe(ctx, scripts), cache_system=True)
    _fail_if(story.validate_wardrobe(plan, ctx["bible"], scripts))
    story.persist_wardrobe(sid, plan)
    return _result([usage], outfits=len(plan.items))


# ---------------------------------------------------------------- originality

def llm_originality(job: dict) -> dict:
    sid = job["series_id"]
    ctx = story.series_context(sid)
    usages: list[dict] = []

    def rewrite(ep_number: int, reasons: list[str]) -> EpisodeScript | None:
        """One more script attempt for the episode, told what was too close to the references."""
        ep = db.fetch_one("select id from episodes where series_id = %s and number = %s", (sid, ep_number))
        note = ("This script was rejected by the originality check:\n- " + "\n- ".join(reasons)
                + "\nKeep the beat, but rewrite with entirely new wording, names and images.")
        try:
            script, usage = generate_script(ctx, ep_number, job, note)
        except PermanentError:
            return None  # the failure stays in the report for the reviewer
        usages.append(usage)
        story.persist_script(sid, ep["id"], script)
        return script

    def load_beats():
        return {r["number"]: EpisodeBeat.model_validate(r["beat"]) for r in db.fetch_all("select number, beat from episodes where series_id = %s", (sid,))}

    report, used = originality.run(ctx, rewrite, load_beats)
    return _result([*usages, *used], passed=report["passed"], same_story=report["same_story"], rewritten=report["rewritten_episodes"])


HANDLERS = {
    "llm_bible": SimpleNamespace(run=llm_bible), "llm_beats": SimpleNamespace(run=llm_beats), "llm_placement_plan": SimpleNamespace(run=llm_placement_plan),
    "llm_script": SimpleNamespace(run=llm_script), "llm_continuity": SimpleNamespace(run=llm_continuity), "llm_wardrobe": SimpleNamespace(run=llm_wardrobe),
    "llm_originality": SimpleNamespace(run=llm_originality),
}
