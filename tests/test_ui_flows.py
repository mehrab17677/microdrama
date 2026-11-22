"""Form errors beside their fields, the two lead photos, the side panel, live progress, and the writer's second chance on a rejected bible."""
import uuid

import pytest
from helpers import at_gate, drive
from test_phase5 import brief, client, env, png, runner, start  # noqa: F401 (fixtures)
from test_phase6 import job_for

from app import db, overview
from app.jobs.handlers import story_handlers as H
from app.stages import story, story_fixtures as fx
from app.vendors import llm

JSON = {"accept": "application/json"}


def lead_form(**over):
    """The page's form: two photo boxes and the hidden leads=2 marker."""
    data, _ = brief()
    data.pop("char_roles")
    data["leads"] = "2"
    data["language"] = "en"
    data.update(over.pop("data", {}))
    files = over.pop("files", [("male_photo", ("m.png", png(), "image/png")), ("female_photo", ("f.png", png((640, 800), (90, 60, 120)), "image/png"))])
    return data, files


# ---------------------------------------------------------------- errors beside their fields

def test_each_problem_comes_back_under_its_own_field_and_the_rest_is_not_blamed(env):
    c = client()
    data, files = lead_form(data={"keywords": "romance", "language": ""}, files=[("male_photo", ("m.png", png(), "image/png"))])
    r = c.post("/productions", data=data, files=files, headers=JSON)
    assert r.status_code == 400
    fields = r.json()["fields"]
    assert set(fields) == {"keywords", "language", "female_photo"}                         # the valid male photo, genre and look are not flagged
    assert "3 to 8 keywords" in fields["keywords"][0] and "female lead" in fields["female_photo"][0]
    assert r.json()["errors"] == [m for ms in fields.values() for m in ms] or len(r.json()["errors"]) == 3   # the older flat list is still there


def test_a_picture_problem_names_the_lead_it_belongs_to(env):
    c = client()
    small = png((200, 200))
    data, files = lead_form(files=[("male_photo", ("m.png", png(), "image/png")), ("female_photo", ("f.png", small, "image/png"))])
    fields = c.post("/productions", data=data, files=files, headers=JSON).json()["fields"]
    assert list(fields) == ["female_photo"] and "The female lead photo is too small" in fields["female_photo"][0]


def test_library_upload_errors_are_per_field(env):
    c = client()
    r = c.post("/library/upload", data={"genre": ""}, headers=JSON)
    assert r.status_code == 400 and set(r.json()["fields"]) == {"genre", "files"}
    r = c.post("/library/upload", data={"genre": "x"}, headers=JSON)
    assert set(r.json()["fields"]) == {"files"}


def test_the_form_pages_use_inline_errors_and_previews(env):
    c = client()
    page = c.get("/productions/new?category=ad_placement").text
    assert "data-inline" in page and 'name="male_photo"' in page and 'name="female_photo"' in page and 'name="leads" value="2"' in page
    assert 'data-preview="brand"' in page and 'name="char_files"' not in page
    assert "data-inline" in c.get("/library").text


# ---------------------------------------------------------------- two leads

def test_two_lead_photos_make_two_labelled_references_and_the_supporting_cast_needs_none(runner):
    c = client()
    data, files = lead_form()
    r = c.post("/productions", data=data, files=files, headers=JSON)
    assert r.status_code == 303
    sid = uuid.UUID(r.headers["location"].rsplit("/", 1)[1])
    roles = sorted(x["role_label"] for x in db.fetch_all("select role_label from brief_refs where series_id = %s and kind = 'character'", (sid,)))
    assert roles == ["female lead", "male lead"]
    drive(runner, sid, at_gate(2))
    slots = [c_["slot_role"] for c_ in db.fetch_one("select content from story_bibles where series_id = %s", (sid,))["content"]["characters"]]
    assert sorted(x for x in slots if x) == ["female lead", "male lead"] and None in slots      # leads from photos, everyone else from the story


def test_the_older_way_of_sending_photos_still_works(env):
    c = client()
    data, files = brief()
    assert c.post("/productions", data=data, files=files, headers=JSON).status_code == 303


# ---------------------------------------------------------------- side panel and live progress

def test_the_side_panel_groups_the_pages_and_shows_the_current_production(runner):
    c = client("admin@x.io")
    sid = start(client(), data={"language": "en"})
    home = c.get("/").text
    for needle in ("Studio", "Dashboard", "Alerts", "Create", "New production", "Library", "Productions", "Admin", "Team"):
        assert needle in home
    assert 'class="side"' in home and f"/productions/{sid}" in home                             # the production is listed in the panel
    page = c.get(f"/productions/{sid}").text
    for needle in ("Overview", "Story review", "Look review", "Episodes review", "Delivery", 'role="progressbar"', 'id="live"'):
        assert needle in page
    member = client().get("/").text
    assert "Team" not in member.split("<main>")[0]                                              # only the admin sees the Admin group


def test_progress_counts_jobs_per_stage_and_draws_a_percentage(runner):
    sid = start(client(), data={"language": "en"})
    drive(runner, sid, at_gate(2))
    s = db.fetch_one("select * from series where id = %s", (sid,))
    p = overview.progress(s)
    stages = {x["key"]: x for x in p["stages"]}
    assert stages["story"]["state"] == "done" and stages["story"]["total"] > 0 and stages["story"]["done"] == stages["story"]["total"]
    assert stages["gate2"]["state"] == "waiting" and stages["visual"]["state"] == "not started"
    assert 0 < p["percent"] < 100 and p["running"] is False and p["failure"] is None
    later = overview.progress({**s, "status": "done", "current_stage": "done"}, overview.stage_jobs(sid))
    assert later["percent"] == 100.0
    db.execute("update series set status = 'failed', current_stage = 'story' where id = %s", (sid,))
    db.execute("update jobs set status = 'failed', error = 'boom' where id = (select id from jobs where series_id = %s and kind = 'llm_bible' limit 1)", (sid,))
    failed = overview.progress(db.fetch_one("select * from series where id = %s", (sid,)))
    assert (failed["failure"]["kind"], failed["failure"]["error"]) == ("llm_bible", "boom") and failed["stages"][2]["state"] == "failed"


def test_the_production_page_and_dashboard_show_the_progress(runner):
    c = client()
    sid = start(c, data={"language": "en"})
    drive(runner, sid, at_gate(2))
    page = c.get(f"/productions/{sid}").text
    assert 'class="overall' in page and 'data-state="done"' in page and 'class="ring"' in page and "jobs" in page
    assert 'class="mini"' in c.get("/").text


# ---------------------------------------------------------------- the writer gets a second chance on a rejected bible

def _bible_calls(monkeypatch, sid, bad_times):
    calls = []
    ctx = story.series_context(sid)
    good = fx.make_bible(ctx)

    def fake(role, system, messages, schema=None, images=None, fixture=None, cache_system=False):
        calls.append(messages)
        draft = good.model_copy(deep=True)
        if len(calls) <= bad_times:
            draft.characters[0].visual_description = "far too short"
        return draft, {"model": "anthropic:claude-sonnet-5-5", "tokens_in": 10, "tokens_out": 10, "cost_usd": 0.01}

    monkeypatch.setattr(llm, "complete", fake)
    return calls


def test_a_rejected_bible_is_corrected_once_inside_the_same_job(env, monkeypatch):
    sid = start(client(), data={"language": "en"})
    calls = _bible_calls(monkeypatch, sid, bad_times=1)
    result = H.llm_bible(job_for(sid, "llm_bible"))
    assert len(calls) == 2 and "rejected by the checks" in calls[1][-1]["content"] and "visual_description" in calls[1][-1]["content"]
    assert "rejected by the checks" not in calls[0][-1]["content"]
    assert result["actual_cost"] == pytest.approx(0.02) and db.fetch_one("select 1 from story_bibles where series_id = %s", (sid,))


def test_a_bible_that_stays_wrong_fails_after_the_allowed_attempts(env, monkeypatch):
    from app.jobs.errors import PermanentError

    sid = start(client(), data={"language": "en"})
    calls = _bible_calls(monkeypatch, sid, bad_times=99)
    with pytest.raises(PermanentError, match="visual_description"):
        H.llm_bible(job_for(sid, "llm_bible"))
    assert len(calls) == H.BIBLE_ATTEMPTS


def test_generic_blocklist_phrases_are_dropped_and_the_writer_is_told_the_rest(env, monkeypatch):
    assert [story.meaningful_term(t) for t in ("the night", "a dog", "ab", "olive", "sloane", "the night shift", "quiet debt")] == [False, False, False, True, True, True, True]
    monkeypatch.setattr(story, "blocklist", lambda sid: ["sloane", "olive"])
    system = story.build_system("Instructions.", {"language": "en", "bible": None, "id": "any"}, with_bible=False)
    assert "Never use any of these names or terms" in system and "olive; sloane" in system
    monkeypatch.setattr(story, "blocklist", lambda sid: [])
    assert "Never use any" not in story.build_system("Instructions.", {"language": "en", "bible": None, "id": "any"}, with_bible=False)
