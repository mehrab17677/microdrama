import json
import uuid
from types import SimpleNamespace

import pytest
from helpers import at_gate, drive, is_status
from test_phase5 import ENV, brief, client, env, gate, jobs_of, post_json, png, runner, start, to_gate, voices  # noqa: F401 (fixtures)

from app import db
from app.config import clear_config_cache
from app.graphs import common
from app.jobs.errors import PermanentError
from app.jobs.handlers import story_handlers as H
from app.models.story import (BeatSheet, CharacterDef, ContinuityEdit, ContinuityReport, EpisodeBeat, EpisodeScript, PlacementItem, PlacementPlan, Scene, ScriptLine,
                              SceneRef, SetDef, StoryBible, WardrobeItemOut, WardrobePlan)
from app.stages import originality, story, story_fixtures as fx
from app.text import normalize_for_match
from app.vendors import images, llm


# ---------------------------------------------------------------- helpers

def job_for(sid, kind, episode_id=None, **request):
    return {"id": uuid.uuid4(), "kind": kind, "series_id": sid, "episode_id": episode_id, "request": {"rev": 0, **request}}


def ctx_of(sid):
    return story.series_context(sid)


def build_story(sid, upto="scripts"):
    """Run the story handlers directly (no graph) up to and including `upto`."""
    H.llm_bible(job_for(sid, "llm_bible"))
    if upto == "bible":
        return
    H.llm_beats(job_for(sid, "llm_beats"))
    if upto == "beats":
        return
    for e in db.fetch_all("select id from episodes where series_id = %s order by number", (sid,)):
        H.llm_script(job_for(sid, "llm_script", e["id"]))


def recorder(monkeypatch):
    """Record every llm.complete call (role, system, messages, schema name, cache flag) and delegate."""
    calls, real = [], llm.complete

    def spy(role, system, messages, schema=None, images=None, fixture=None, cache_system=False):
        calls.append(SimpleNamespace(role=role, system=system, messages=messages, schema=schema and schema.__name__, cache=cache_system))
        return real(role, system, messages, schema, images, fixture, cache_system)

    monkeypatch.setattr(llm, "complete", spy)
    return calls


def bible(roles=("female lead",), episodes=45, sets=4, **over) -> StoryBible:
    ctx = {"roles": list(roles), "language": "bn", "bible": None}
    b = fx.make_bible(ctx)
    data = {**b.model_dump(), "episode_count": episodes, **over}
    data["sets"] = data["sets"][:sets] if sets <= 4 else data["sets"] + [{**data["sets"][0], "code": f"SET{i:02d}"} for i in range(5, sets + 1)]
    return StoryBible.model_validate(data)


# ---------------------------------------------------------------- validators (pure)

def test_a_valid_bible_has_no_problems():
    assert story.validate_bible(bible(), ["female lead"], []) == []


def test_bible_rules(env):
    db.execute("update config set value = '[45, 50]'::jsonb where key = 'episode_count_range'")      # the real range
    clear_config_cache()
    assert any("must appear exactly once" in p for p in story.validate_bible(bible(), ["female lead", "rival"], []))
    two = bible()
    two.characters[1].slot_role = "female lead"
    assert any("found 2" in p for p in story.validate_bible(two, ["female lead"], []))
    assert any("is not one of the intake" in p for p in story.validate_bible(bible(roles=("villain",)), ["female lead"], []))
    b = bible()
    b.sets = b.sets[:3]
    assert any("4 to 8 sets, not 3" in p for p in story.validate_bible(b, ["female lead"], []))
    assert not any("sets" in p for p in story.validate_bible(bible(sets=8), ["female lead"], []))
    assert any("not 9" in p for p in story.validate_bible(bible(sets=9), ["female lead"], []))
    for n, ok in ((44, False), (45, True), (50, True), (51, False)):
        assert any("episode_count" in p for p in story.validate_bible(bible(episodes=n), ["female lead"], [])) is (not ok)
    dup = bible()
    dup.characters[1].code = "CH01"
    assert any("character codes" in p for p in story.validate_bible(dup, ["female lead"], []))
    bad_set = bible()
    bad_set.sets[0].times_of_day = ["noon"]
    assert any("times_of_day" in p for p in story.validate_bible(bad_set, ["female lead"], []))
    nolead = bible()
    for c in nolead.characters:
        c.is_lead = False
    assert any("at least one character must be a lead" in p for p in story.validate_bible(nolead, ["female lead"], []))
    short = bible()
    short.characters[0].visual_description = "too short"
    assert any("must be 40 to 80" in p for p in story.validate_bible(short, ["female lead"], []))
    assert any("reference material: lead 1" in p for p in story.validate_bible(bible(), ["female lead"], ["lead 1"]))


def test_beat_chunk_rules():
    b = bible()
    ctx = {"bible": b}
    beats = fx.make_beats(ctx, 1, 3).episodes
    assert story.validate_beat_chunk(beats, 1, 3, b, []) == []
    assert any("with no gaps" in p for p in story.validate_beat_chunk(beats[:2] + [beats[2].model_copy(update={"number": 9})], 1, 3, b, []))
    broken = [beats[0].model_copy(update={"hook": " ", "character_codes": ["CH77"], "set_codes": ["SET99"], "handoff": {"CH88": "x"}})]
    text = " | ".join(story.validate_beat_chunk(broken, 1, 1, b, []))
    for needle in ("hook and a cliffhanger", "unknown character CH77", "unknown character CH88", "unknown set SET99"):
        assert needle in text
    assert any("reference material" in p for p in story.validate_beat_chunk(beats, 1, 3, b, ["debt plot"]))


def script(lines_per_scene=5, words=12, language="bn", scenes=3, **over) -> EpisodeScript:
    ctx = {"language": language, "bible": bible()}
    s = fx.make_script(ctx, 1)
    data = s.model_dump()
    for sc in data["scenes"]:
        for l in sc["lines"]:
            l["text"] = fx._line_text(language, 1, words)
    return EpisodeScript.model_validate({**data, **over})


def test_runtime_estimate_follows_the_guide():
    s = script()
    words = sum(len(l.text.split()) for sc in s.scenes for l in sc.lines)
    assert words == 180
    assert story.estimate_runtime(s) == pytest.approx(180 / 2.5 + 5 * 2.0)                 # 72 + 5 action sentences x 2 s = 82
    assert story.sentence_count("One. Two! Three? ") == 3 and story.sentence_count("") == 0 and story.sentence_count("আমি যাই। তুমি এসো।") == 2


def test_script_rules():
    b = bible()
    assert story.validate_script(script(), 1, b, "bn") == []
    assert any("is for episode 2, not 1" in p for p in story.validate_script(script(), 2, b, "bn"))
    assert any("limit is 20" in p for p in story.validate_script(script(words=25), 1, b, "bn"))
    assert any("estimated runtime" in p and "add dialogue" in p for p in story.validate_script(script(words=6), 1, b, "bn"))
    long = script(words=20)                                         # 300 words / 2.5 + 10 = 130 s
    assert story.estimate_runtime(long) == pytest.approx(130) and any("cut dialogue" in p for p in story.validate_script(long, 1, b, "bn"))
    en = script(language="en")
    assert any("not written in the series language" in p for p in story.validate_script(en, 1, b, "bn"))
    assert story.validate_script(en, 1, b, "en") == []
    assert any("not written in the series language" in p for p in story.validate_script(script(), 1, b, "en"))      # Bangla for an English series
    s = script()
    s.scenes[0].lines[0].character_code = "CH99"
    s.scenes[1].set_code = "SET09"
    s.scenes[2].set_code, s.scenes[2].time_of_day = "SET03", "night"                                              # SET03 only has day
    text = " | ".join(story.validate_script(s, 1, b, "bn"))
    for needle in ("CH99 is not a speaking character", "unknown set SET09", "has no 'night' time"):
        assert needle in text
    mute = bible()
    mute.characters[0].speaks = False
    assert any("not a speaking character" in p for p in story.validate_script(script(), 1, mute, "bn"))
    assert any("no dialogue" in p for p in story.validate_script(script(scenes=3).model_copy(update={"scenes": [sc.model_copy(update={"lines": []}) for sc in script().scenes]}), 1, b, "bn"))


def test_wardrobe_rules():
    b = bible()
    scripts = {1: script(), 2: script()}
    plan = fx.make_wardrobe({"bible": b}, scripts)
    assert story.validate_wardrobe(plan, b, scripts) == []
    thin = WardrobePlan(items=[plan.items[0].model_copy(update={"scenes": [SceneRef(episode=1, scene=0)]}), *plan.items[1:]])
    assert any("speaks in scenes with no outfit" in p for p in story.validate_wardrobe(thin, b, scripts))
    many = WardrobePlan(items=[plan.items[0].model_copy(update={"code": f"W{i:02d}"}) for i in range(1, 6)] + plan.items[1:])
    assert any("5 outfits; the limit is 4" in p for p in story.validate_wardrobe(many, b, scripts))
    support_code = b.characters[-1].code
    two = WardrobePlan(items=[*plan.items, WardrobeItemOut(character_code=support_code, code="W02", description="x", scenes=[])])
    assert any(f"{support_code} has 2 outfits; the limit is 1" in p for p in story.validate_wardrobe(two, b, scripts))
    ghost = WardrobePlan(items=[*plan.items, WardrobeItemOut(character_code="CH50", code="W01", description="x", scenes=[])])
    assert any("unknown character CH50" in p for p in story.validate_wardrobe(ghost, b, scripts))


def test_placement_rules():
    brand = {"placement_types": ["phone_full", "spoken"], "exclusions": ["scam", "crime"]}
    summaries = {1: "A quiet dinner.", 2: "The scam is exposed.", 3: "A chase."}
    ok = PlacementPlan(placements=[PlacementItem(episode=1, kind="phone_full", description="d")])
    assert story.validate_placement(ok, brand, summaries) == []
    text = " | ".join(story.validate_placement(PlacementPlan(placements=[PlacementItem(episode=2, kind="signage", description="d"), PlacementItem(episode=9, kind="spoken", description="d")]), brand, summaries))
    for needle in ("'signage' is not allowed", "matches the exclusion 'scam'", "unknown episode 9"):
        assert needle in text
    assert story.validate_placement(PlacementPlan(placements=[]), brand, summaries) == ["choose at least one placement"]


def test_continuity_edits_that_do_not_fit_are_skipped_not_guessed(env):
    scripts = {1: script()}
    report = ContinuityReport(edits=[
        ContinuityEdit(episode=1, scene=0, line_index=0, new_text=fx._line_text("bn", 3, 10), reason="name"),
        ContinuityEdit(episode=1, scene=0, line_index=99, new_text="x", reason="x"),
        ContinuityEdit(episode=7, scene=0, line_index=0, new_text="x", reason="x"),
        ContinuityEdit(episode=1, scene=1, line_index=0, new_text=" ".join(["শব্দ"] * 25), reason="long"),
        ContinuityEdit(episode=1, scene=1, line_index=1, new_text="English words only here", reason="language"),
    ])
    valid, skipped = story.validate_continuity(report, scripts, "bn")
    assert len(valid) == 1 and len(skipped) == 4


def test_blocklist_matching_helpers():
    obj = {"a": ["Kabir went home"], "b": {"c": "no match here"}}
    assert story.all_strings(obj) == ["Kabir went home", "no match here"]
    assert story.blocklist_hits(obj, ["kabir", "ali", "home"]) == ["home", "kabir"]
    assert story.blocklist_hits({"x": "Alice went"}, ["ali"]) == []                     # whole words only for Latin terms
    assert story.blocklist_hits({"x": "সে কবীরকে ডাকল"}, [normalize_for_match("কবীর")]) == [normalize_for_match("কবীর")]


# ---------------------------------------------------------------- the llm wrapper

def test_llm_wrapper_additions(env, monkeypatch):
    assert llm.max_tokens_for("story") == 16000 and llm.max_tokens_for("rule_writer") == 500 and llm.max_tokens_for("nonsense") == 8000
    msgs = llm._build_messages("SYSTEM", [{"role": "user", "content": "hi"}], None, cache_system=True)
    assert msgs[0]["content"] == [{"type": "text", "text": "SYSTEM", "cache_control": {"type": "ephemeral"}}]
    assert llm._build_messages("SYSTEM", [{"role": "user", "content": "hi"}], None)[0]["content"] == "SYSTEM"
    monkeypatch.setenv("DRY_RUN", "1")
    obj, usage = llm.complete("story", "s", [], schema=StoryBible, fixture=lambda: bible())
    assert isinstance(obj, StoryBible) and usage["cost_usd"] == 0
    obj, _ = llm.complete("story", "s", [], schema=StoryBible, fixture=bible().model_dump())      # a plain dict is validated into the schema
    assert isinstance(obj, StoryBible)


# ---------------------------------------------------------------- the stage, end to end in dry-run

def test_the_story_stage_writes_every_artifact(runner):
    c = client()
    sid = start(c)
    drive(runner, sid, at_gate(2))
    s = db.fetch_one("select title, episode_count from series where id = %s", (sid,))
    assert s["title"] == "The Quiet Debt" and s["episode_count"] == 5
    assert db.fetch_one("select count(*) n from story_bibles where series_id = %s", (sid,))["n"] == 1
    chars = db.fetch_all("select code, is_lead from characters where series_id = %s order by code", (sid,))
    assert [(r["code"], r["is_lead"]) for r in chars] == [("CH01", True), ("CH02", False)]
    assert db.fetch_one("select count(*) n from sets where series_id = %s", (sid,))["n"] == 6          # 4 sets, two with two times of day
    eps = db.fetch_all("select number, batch, status, script is not null has_script from episodes where series_id = %s order by number", (sid,))
    assert [(e["number"], e["batch"], e["status"], e["has_script"]) for e in eps] == [(1, "pilot", "scripted", True), (2, "pilot", "scripted", True), (3, "pilot", "scripted", True),
                                                                                    (4, "full", "scripted", True), (5, "full", "scripted", True)]
    assert db.fetch_one("select count(*) n from dialogue_lines where series_id = %s", (sid,))["n"] == 75
    assert db.fetch_one("select count(*) n from wardrobe_items where series_id = %s", (sid,))["n"] == 2
    lines = db.fetch_all("select text from dialogue_lines where series_id = %s", (sid,))
    assert all(any("ঀ" <= ch <= "৿" for ch in l["text"]) for l in lines)                      # the series language is Bangla
    report = db.fetch_one("select passed, detail from qa_results where series_id = %s and check_name = 'originality'", (sid,))
    assert report["passed"] is True and report["detail"]["same_story"] is False
    assert [j["status"] for j in jobs_of(sid, "llm_bible")] == ["succeeded"]


# ---------------------------------------------------------------- acceptance: redo at most twice

def test_validation_failures_trigger_a_redo_at_most_twice_and_the_next_attempt_sees_the_errors(runner, monkeypatch):
    seen = recorder(monkeypatch)
    real, calls = fx.make_script, {}

    def flaky(ctx, number):
        calls[number] = calls.get(number, 0) + 1
        s = real(ctx, number)
        if number == 2 and calls[number] <= 2:
            s.scenes[0].lines[0].text = " ".join(["word"] * 25)                      # too long, and not Bangla
        return s

    monkeypatch.setattr(fx, "make_script", flaky)
    sid = start(client())
    drive(runner, sid, at_gate(2))
    two = [j for j in jobs_of(sid, "llm_script") if j["request"]["episode"] == 2]
    assert [(j["attempt"], j["status"]) for j in two] == [(1, "failed"), (2, "failed"), (3, "succeeded")]
    assert "limit is 20" in two[0]["error"] and "series language" in two[0]["error"]
    prompts = [c.messages[0]["content"] for c in seen if c.schema == "EpisodeScript" and "episode 2." in c.messages[0]["content"]]
    assert len(prompts) == 3 and "previous attempt was rejected" not in prompts[0]
    assert "previous attempt was rejected" in prompts[1] and "limit is 20" in prompts[1]
    assert prompts[2].count("- ") >= 2 and "previous attempt was rejected" in prompts[2]


def test_a_script_that_never_validates_stops_the_series_after_three_attempts(runner, monkeypatch):
    real = fx.make_script

    def always_bad(ctx, number):
        s = real(ctx, number)
        s.scenes[0].lines[0].text = " ".join(["word"] * 25)
        return s

    monkeypatch.setattr(fx, "make_script", always_bad)
    sid = start(client())
    drive(runner, sid, is_status("failed"))
    first = jobs_of(sid, "llm_script")
    assert sorted({j["attempt"] for j in first}) == [1, 2, 3] and all(j["status"] == "failed" for j in first)
    assert "script" in db.fetch_all("select message from alerts where kind = 'step_failed'")[0]["message"]
    assert jobs_of(sid, "llm_continuity") == []


# ---------------------------------------------------------------- acceptance: originality

def add_source(language="en", lore=7, genre=None, series_id=None, terms=(), chunks=()):
    sid_ = db.fetch_one(
        "insert into sources (genre_id, series_id, batch_id, file_name, file_sha256, r2_key, language, status, lore_version, plot_summary) values (%s,%s,%s,%s,%s,'k',%s,'ready',%s,'A plot.') returning id",
        (genre if genre is not None else (None if series_id else ENV["genre"]), series_id, uuid.uuid4(), "s.txt", uuid.uuid4().hex, language, lore))["id"]
    for t in terms:
        db.execute("insert into blocklist_terms (source_id, term, term_normalized, kind) values (%s,%s,%s,'invented')", (sid_, t, normalize_for_match(t)))
    for i, text in enumerate(chunks):
        vec = images.embed([text])[0][0]
        db.execute("insert into source_chunks (source_id, chapter_index, text, embedding) values (%s,%s,%s,%s::vector)", (sid_, i, text, db.to_vector(vec)))
    return sid_


def test_a_script_containing_a_blocklist_term_fails_originality(runner, monkeypatch):
    add_source(terms=["tonight"])                                       # a word the fixture dialogue uses
    seen = recorder(monkeypatch)
    sid = start(client(), data={"language": "en"})
    drive(runner, sid, at_gate(2))
    report = db.fetch_one("select passed, detail from qa_results where series_id = %s and check_name = 'originality'", (sid,))
    assert report["passed"] is False
    hits = report["detail"]["blocklist"]
    assert hits and all("tonight" in h["terms"] for h in hits)
    assert report["detail"]["rewritten_episodes"] == [1, 2, 3, 4, 5]    # each failing episode got exactly one more attempt
    rewrites = [c for c in seen if c.schema == "EpisodeScript" and "originality check" in c.messages[0]["content"]]
    assert len(rewrites) == 5 and "tonight" in rewrites[0].messages[0]["content"]
    assert len(jobs_of(sid, "llm_script")) == 5                         # the rewrite is inside the originality job, not a new job
    page = client().get(f"/productions/{sid}/review/story")
    assert "Needs attention." in page.text and "tonight" in page.text


def test_a_rewrite_that_removes_the_term_passes(runner, monkeypatch):
    add_source(terms=["tonight"])
    real, first = fx.make_script, set()

    def clean_on_rewrite(ctx, number):
        s = real(ctx, number)
        if number in first:                                              # the second call for an episode is the rewrite
            for sc in s.scenes:
                for l in sc.lines:
                    l.text = l.text.replace("tonight", "later").replace("রাতে", "দিনে")
        first.add(number)
        return s

    monkeypatch.setattr(fx, "make_script", clean_on_rewrite)
    sid = start(client(), data={"language": "en"})                      # English: the fixture line contains the word "tonight"
    drive(runner, sid, at_gate(2))
    report = db.fetch_one("select passed, detail from qa_results where series_id = %s and check_name = 'originality'", (sid,))
    assert report["passed"] is True and report["detail"]["blocklist"] == [] and report["detail"]["rewritten_episodes"] == [1, 2, 3, 4, 5]
    assert "tonight" not in " ".join(r["text"] for r in db.fetch_all("select text from dialogue_lines where series_id = %s", (sid,)))


def test_blocklist_in_the_bible_fails_the_bible(runner):
    add_source(terms=["Quiet Debt"])                                    # the fixture title
    sid = start(client())
    drive(runner, sid, is_status("failed"))
    assert "reference material" in jobs_of(sid, "llm_bible")[0]["error"] and len(jobs_of(sid, "llm_bible")) == 3
    assert jobs_of(sid, "llm_beats") == []


def make_ctx_series(language="en"):
    """A bare series (no graph) to unit-test the checks."""
    sid = start(client(), data={"language": language})
    return sid


def test_wording_check_finds_eight_identical_words_in_a_row(env):
    sid = make_ctx_series("en")
    sc = fx.make_script({"language": "en", "bible": bible()}, 1)
    first_scene_text = originality.scene_text(sc.scenes[0])
    run = " ".join(first_scene_text.split()[3:14])                       # eleven words from inside the scene
    add_source(chunks=[f"Once upon a time {run} and then something else entirely happened."], language="en")
    near = " ".join(first_scene_text.split()[3:10]) + " then a different ending follows here"   # only seven in a row
    add_source(chunks=[near], language="en", lore=7)
    ctx = ctx_of(sid)
    hits = originality.check_wording(ctx, {1: sc})
    assert list(hits) == [(1, 0)] and len(hits[(1, 0)]) == 1
    other_language = add_source(chunks=[f"x {run} y"], language="bn")      # a different language is not compared
    assert str(other_language) not in {s for v in originality.check_wording(ctx, {1: sc}).values() for s in v}


def test_similarity_check_uses_nearest_chunk_in_the_same_language_and_pinned_version(env):
    sid = make_ctx_series("en")
    sc = fx.make_script({"language": "en", "bible": bible()}, 1)
    text = originality.scene_text(sc.scenes[1])
    add_source(chunks=[text], language="en", lore=99)                    # newer than the pinned lore version 7: ignored
    add_source(chunks=[text], language="bn")                             # other language: ignored
    add_source(chunks=[text], language="en", genre=db.fetch_one("insert into genres (name, name_normalized) values ('Other','other') returning id")["id"])
    ctx = ctx_of(sid)
    assert originality.check_similarity(ctx, {1: sc})[0] == {}
    src = add_source(chunks=[text], language="en")
    found, usages = originality.check_similarity(ctx, {1: sc})
    assert list(found) == [(1, 1)] and found[(1, 1)][0] > 0.99 and found[(1, 1)][1] == str(src)
    db.execute("update config set value = '1.5'::jsonb where key = 'originality_cosine_max'")
    clear_config_cache()
    assert originality.check_similarity(ctx, {1: sc})[0] == {}           # the limit is a config value


def test_same_story_is_reported_at_gate_2_and_not_auto_fixed(runner, monkeypatch):
    from app.models.story import PlotCheck

    monkeypatch.setattr(fx, "make_plot_check", lambda: PlotCheck(same_story=True, nearest_source_id=None, reason="Same revenge plot with new names."))
    add_source(chunks=["A story chunk about something else."], language="bn")   # another language is still compared for plot
    sid = start(client())
    drive(runner, sid, at_gate(2))
    report = db.fetch_one("select passed, detail from qa_results where series_id = %s and check_name = 'originality'", (sid,))
    assert report["passed"] is False and report["detail"]["same_story"] is True and report["detail"]["rewritten_episodes"] == []
    assert len(jobs_of(sid, "llm_script")) == 5 and len(jobs_of(sid, "llm_originality")) == 1
    assert "Same revenge plot" in client().get(f"/productions/{sid}/review/story").text


# ---------------------------------------------------------------- inputs: retrieval and prompts

def test_card_retrieval_is_scoped_to_genre_lore_version_and_own_story(env):
    sid = make_ctx_series("en")
    ctx = ctx_of(sid)
    query = f"{ctx['genre_name']}. " + ", ".join(ctx["brief"]["keywords"])
    qvec = db.to_vector(images.embed([query])[0][0])
    other_genre = db.fetch_one("insert into genres (name, name_normalized) values ('Other','other') returning id")["id"]
    own = add_source(series_id=sid, lore=None)

    def card(source, title, vec=None, genre=ENV["genre"]):
        v = vec or db.to_vector(images.embed([title])[0][0])
        db.execute("insert into pattern_cards (source_id, genre_id, kind, title, body, embedding) values (%s,%s,'trope',%s,'body',%s::vector)", (source, genre, title, v))

    current, newer, elsewhere = add_source(lore=7), add_source(lore=9), add_source(genre=other_genre, lore=5)
    card(current, "Exact match", qvec)
    card(current, "Revenge in a small town")
    card(newer, "From a newer lore version")
    card(elsewhere, "From another genre", genre=other_genre)
    card(own, "From the user's own story", genre=None)
    cards, usage = story.retrieve_cards(ctx)
    titles = [c["title"] for c in cards]
    assert titles[0] == "Exact match" and set(titles) == {"Exact match", "Revenge in a small town", "From the user's own story"}
    assert len(story.retrieve_cards(ctx, limit=1)[0]) == 1


def test_prompt_order_and_a_shared_cacheable_prefix(env, monkeypatch):
    seen = recorder(monkeypatch)
    sid = start(client())
    build_story(sid, "scripts")
    scripts = [c for c in seen if c.schema == "EpisodeScript"]
    assert len(scripts) == 5 and all(c.cache for c in scripts) and len({c.system for c in scripts}) == 1       # identical prefix: it caches
    system = scripts[0].system
    assert system.index("You are the Screenwriter") < system.index("## Story Bible (JSON)") < system.index("## Series summary")   # guide 9.2 order
    assert "Dialogue language: Bangla" in system
    assert "Write the script for episode" in scripts[0].messages[0]["content"] and "Write the script for episode" not in system
    bible_call = next(c for c in seen if c.schema == "StoryBible")
    assert "## Story Bible" not in bible_call.system and not bible_call.cache                                   # none exists yet; the intake roles are in the message
    assert '"female lead"' in bible_call.messages[0]["content"] and "revenge, small town, slow burn" in bible_call.messages[0]["content"]


def test_beats_are_requested_in_chunks_and_each_sees_the_one_before(env, monkeypatch):
    db.execute("update config set value = '40'::jsonb where key = 'dryrun_episode_count'")
    clear_config_cache()
    seen = recorder(monkeypatch)
    sid = start(client())
    build_story(sid, "beats")
    beat_calls = [c for c in seen if c.schema == "BeatSheetOut"]
    assert len(beat_calls) == 3
    assert [("episodes 1 to 15" in c.messages[0]["content"], "episodes 16 to 30" in c.messages[0]["content"], "episodes 31 to 40" in c.messages[0]["content"]) for c in beat_calls] == [
        (True, False, False), (False, True, False), (False, False, True)]
    assert "The episodes so far" not in beat_calls[0].messages[0]["content"] and "14. Episode 14" in beat_calls[1].messages[0]["content"]
    assert db.fetch_one("select count(*) n, min(number) lo, max(number) hi from episodes where series_id = %s", (sid,)) == {"n": 40, "lo": 1, "hi": 40}


def test_bible_retrieval_failures_are_not_swallowed(env, monkeypatch):
    sid = start(client())

    def broken(texts):
        raise PermanentError("HTTP 429: You have no credits remaining")

    monkeypatch.setattr(images, "embed", broken)
    with pytest.raises(PermanentError, match="no credits"):
        H.llm_bible(job_for(sid, "llm_bible"))


# ---------------------------------------------------------------- continuity, wardrobe, placement

def test_continuity_edits_are_applied_to_the_script_and_the_dialogue_lines(env, monkeypatch):
    sid = start(client())
    build_story(sid)
    new = fx._line_text("bn", 5, 9)
    monkeypatch.setattr(fx, "make_continuity", lambda: ContinuityReport(edits=[ContinuityEdit(episode=2, scene=1, line_index=2, new_text=new, reason="the name changed"),
                                                                                ContinuityEdit(episode=2, scene=1, line_index=50, new_text="x", reason="bad")]))
    out = H.llm_continuity(job_for(sid, "llm_continuity"))
    assert out["edits"] == 1 and len(out["skipped"]) == 1
    ep = db.fetch_one("select id, script from episodes where series_id = %s and number = 2", (sid,))
    assert ep["script"]["scenes"][1]["lines"][2]["text"] == new
    assert db.fetch_one("select text from dialogue_lines where episode_id = %s and idx = 7", (ep["id"],))["text"] == new          # scene 1, line 2 = line 5 + 2
    assert db.fetch_one("select count(*) n from dialogue_lines where episode_id = %s", (ep["id"],))["n"] == 15


def test_a_wardrobe_plan_that_misses_scenes_is_retried_with_the_errors(runner, monkeypatch):
    seen = recorder(monkeypatch)
    real, calls = fx.make_wardrobe, []

    def thin_first(ctx, scripts):
        plan = real(ctx, scripts)
        calls.append(1)
        if len(calls) == 1:
            plan.items[0].scenes = plan.items[0].scenes[:1]
        return plan

    monkeypatch.setattr(fx, "make_wardrobe", thin_first)
    sid = start(client())
    drive(runner, sid, at_gate(2))
    jobs = jobs_of(sid, "llm_wardrobe")
    assert [(j["attempt"], j["status"]) for j in jobs] == [(1, "failed"), (2, "succeeded")]
    retry = [c for c in seen if c.schema == "WardrobePlan"][1].messages[0]["content"]
    assert "speaks in scenes with no outfit assigned" in retry


def ad_form(exclusions="scams"):
    svg = b"<svg xmlns='http://www.w3.org/2000/svg'><rect width='4' height='4'/></svg>"
    data, files = brief(data={"category": "ad_placement", "brand_name": "Acme", "product": "Pay app", "placement_types": ["phone_full", "spoken"], "frequency_note": "every 3rd episode",
                              "exclusions": exclusions, "brand_kinds": ["app_screen"], "brand_labels": ["Home screen"]})
    files.append(("brand_files", ("home.svg", svg, "image/svg+xml")))
    return data, files


def test_an_ad_placement_plan_is_validated_and_stored(runner):
    c = client()
    data, files = ad_form()
    sid = uuid.UUID(post_json(c, data, files).headers["location"].rsplit("/", 1)[1])
    drive(runner, sid, at_gate(2))
    rows = db.fetch_all("select p.episode_number, p.kind, p.brand_asset_id, a.kind akind from placements p left join brand_assets a on a.id = p.brand_asset_id where p.series_id = %s order by 1", (sid,))
    assert [(r["episode_number"], r["kind"], r["akind"]) for r in rows] == [(2, "phone_full", "app_screen"), (5, "phone_full", "app_screen")]
    assert db.fetch_one("select beat->'placement'->>'kind' k from episodes where series_id = %s and number = 2", (sid,))["k"] == "phone_full"
    assert db.fetch_one("select beat->'placement' p from episodes where series_id = %s and number = 1", (sid,))["p"] is None
    assert "How the brand appears" in c.get(f"/productions/{sid}/review/story").text
    assert db.fetch_one("select count(*) n from jobs where series_id = %s and kind = 'llm_placement_plan' and status = 'succeeded'", (sid,))["n"] == 1


def test_a_placement_in_an_excluded_episode_is_refused(runner):
    c = client()
    data, files = ad_form(exclusions="debt plot")                         # every fixture episode summary matches
    sid = uuid.UUID(post_json(c, data, files).headers["location"].rsplit("/", 1)[1])
    drive(runner, sid, is_status("failed"))
    assert "matches the exclusion 'debt plot'" in jobs_of(sid, "llm_placement_plan")[0]["error"] and len(jobs_of(sid, "llm_placement_plan")) == 3


# ---------------------------------------------------------------- Gate 2

def test_a_gate_2_rejection_of_the_scripts_reaches_the_prompt_and_redoes_only_scripts(runner, monkeypatch):
    c = client()
    sid = start(c)
    drive(runner, sid, at_gate(2))
    seen = recorder(monkeypatch)
    assert gate(c, sid, 2, decision="reject", notes="make the dialogue sharper", target="scripts").status_code == 303
    drive(runner, sid, at_gate(2))
    assert not [x for x in seen if x.schema in ("StoryBible", "BeatSheetOut")]
    scripts = [x for x in seen if x.schema == "EpisodeScript"]
    assert len(scripts) >= 5 and all("make the dialogue sharper" in x.messages[0]["content"] for x in scripts[:5])
    assert len([x for x in seen if x.schema == "WardrobePlan"]) == 1 and len([x for x in seen if x.schema == "ContinuityReport"]) == 1


def test_the_review_page(runner):
    c = client()
    sid = start(c)
    assert client("other@x.io").get(f"/productions/{sid}/review/story").status_code == 200
    assert "not written yet" in c.get(f"/productions/{sid}/review/story").text
    drive(runner, sid, at_gate(2))
    page = c.get(f"/productions/{sid}/review/story")
    for needle in ("The Quiet Debt", "A woman returns to settle an old debt.", "CH01", "SET01", "Dark jacket over a cream blouse.", "Passed.", "Episode 3", "A letter arrives.",
                   "Approve the story", "Reject", "scene", 'name="target"'):
        assert needle in page.text, needle
    assert "Approve the story" not in client("other@x.io").get(f"/productions/{sid}/review/story").text      # only deciders see the controls
    assert "Review the story" in c.get(f"/productions/{sid}").text
    assert c.get(f"/productions/{uuid.uuid4()}/review/story").status_code == 404
    assert gate(c, sid, 2, decision="approve").status_code == 303
    assert "Approve the story" not in c.get(f"/productions/{sid}/review/story").text                           # the gate is closed now


def test_a_beat_chunk_that_stops_early_keeps_its_valid_start_and_asks_only_for_the_rest(env, monkeypatch):
    sid = start(client())
    H.llm_bible(job_for(sid, "llm_bible"))
    real, asked = fx.make_beats, []

    def short_first(ctx, start_, end_):
        asked.append((start_, end_))
        return real(ctx, start_, end_ - 2 if len(asked) == 1 else end_)             # the first answer stops two episodes early

    monkeypatch.setattr(fx, "make_beats", short_first)
    H.llm_beats(job_for(sid, "llm_beats"))
    assert asked == [(1, 5), (4, 5)]                                                  # then only episodes 4 and 5 were requested
    assert db.fetch_one("select count(*) n from episodes where series_id = %s", (sid,))["n"] == 5


def test_a_bad_beat_chunk_is_retried_with_the_errors_before_the_job_fails(env, monkeypatch):
    sid = start(client())
    H.llm_bible(job_for(sid, "llm_bible"))
    seen, real, calls = recorder(monkeypatch), fx.make_beats, []

    def gap_then_ok(ctx, a, b):
        calls.append(1)
        sheet = real(ctx, a, b)
        if len(calls) == 1:
            sheet.episodes[0].number = 9                                              # nothing valid to keep
        return sheet

    monkeypatch.setattr(fx, "make_beats", gap_then_ok)
    H.llm_beats(job_for(sid, "llm_beats"))
    msgs = [c.messages[0]["content"] for c in seen if c.schema == "BeatSheetOut"]
    assert len(msgs) == 2 and "was rejected" in msgs[1] and "with no gaps" in msgs[1]
    monkeypatch.setattr(fx, "make_beats", lambda ctx, a, b: BeatSheet(episodes=[e.model_copy(update={"hook": ""}) for e in real(ctx, a, b).episodes]))
    with pytest.raises(PermanentError, match="failed validation 3 times"):
        H.llm_beats(job_for(sid, "llm_beats"))
