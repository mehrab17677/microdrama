"""Retry after a step has used up its automatic attempts, and the word rule for character descriptions."""
import pytest
from helpers import at_gate, drive, is_status
from test_phase5 import client, env, jobs_of, runner, start  # noqa: F401 (fixtures)
from test_phase6 import add_source, bible

from app import db, retry
from app.graphs import common
from app.stages import story


def failed_bible_production(runner):
    add_source(terms=["Quiet Debt"])                                     # the fixture title: the bible fails its originality rule every time
    sid = start(client())
    drive(runner, sid, is_status("failed"))
    return sid


def test_a_failed_step_shows_its_reason_and_a_retry_button_to_its_owner_only(runner):
    sid = failed_bible_production(runner)
    page = client().get(f"/productions/{sid}").text
    assert 'id="failure"' in page and "Story bible" in page and "reference material" in page and f'action="/productions/{sid}/retry"' in page and "Retry" in page
    other = client("other@x.io")
    assert other.get(f"/productions/{sid}").text.count(f"/productions/{sid}/retry") == 0        # a member who does not own it sees no button
    assert other.post(f"/productions/{sid}/retry").status_code in (403, 404)
    admin = client("admin@x.io")
    assert f'action="/productions/{sid}/retry"' in admin.get(f"/productions/{sid}").text


def test_retry_restarts_at_the_failed_step_with_a_fresh_set_of_attempts(runner):
    sid = failed_bible_production(runner)
    assert len(jobs_of(sid, "llm_bible")) == 3
    c = client()
    r = c.post(f"/productions/{sid}/retry")
    assert r.status_code == 303
    assert db.fetch_one("select status from series where id = %s", (sid,))["status"] == "running"
    cmd = db.fetch_one("select kind, payload from graph_commands where series_id = %s order by created_at desc limit 1", (sid,))
    assert cmd["kind"] == "retry" and cmd["payload"]["node"] == "enq_bible" and cmd["payload"]["step"] == "bible"
    drive(runner, sid, is_status("failed"))                               # still failing (the rule is unchanged), but it tried again, three more times
    assert len(jobs_of(sid, "llm_bible")) == 6 and common.retry_epoch(sid, "bible") == 1
    assert db.fetch_one("select count(*) n from audit_log where series_id = %s and action = 'series.retry'", (sid,))["n"] == 1
    assert db.fetch_one("select status from graph_commands where kind = 'retry' and series_id = %s", (sid,))["status"] == "done"


def test_a_retry_can_succeed_once_the_cause_is_fixed(runner):
    sid = failed_bible_production(runner)
    db.execute("delete from blocklist_terms")                              # the person fixed what blocked it
    client().post(f"/productions/{sid}/retry")
    drive(runner, sid, at_gate(2))
    assert db.fetch_one("select 1 from story_bibles where series_id = %s", (sid,))


def test_only_a_failed_production_can_be_retried(runner):
    sid = start(client())
    assert client().post(f"/productions/{sid}/retry").status_code == 409
    drive(runner, sid, at_gate(2))
    assert client().post(f"/productions/{sid}/retry").status_code == 409


def test_every_retry_target_is_a_real_graph_node(runner):
    nodes = set(runner.graph.get_graph().nodes)
    for step in retry.ENQ_STEPS:
        assert f"enq_{step}" in nodes and retry.retry_node(step) == f"enq_{step}"
    for node in retry.CHECK_NODES:
        assert node in nodes and retry.retry_node(node) == node
    assert retry.retry_node("graph") is None and retry.step_label("graph") and retry.step_label("shot_lists") == "Shot lists"


def test_a_retry_restarts_the_redo_counters_of_the_check_steps(env):
    sid = start(client())
    common.add_note(sid, "shots_redo:1", "a")
    common.add_note(sid, "shots_redo:1", "b")
    assert common.count_notes_since(sid, "shots_redo:1", common.retry_since(sid, "validate_shots", "fit_durations")) == 2
    db.execute("insert into audit_log (series_id, action, detail) values (%s, 'note:retry:validate_shots', '{}'::jsonb)", (sid,))
    assert common.count_notes_since(sid, "shots_redo:1", common.retry_since(sid, "validate_shots", "fit_durations")) == 0
    common.add_note(sid, "shots_redo:1", "c")
    assert common.count_notes_since(sid, "shots_redo:1", common.retry_since(sid, "validate_shots", "fit_durations")) == 1


# ---------------------------------------------------------------- 35 to 80 words

def test_a_character_description_needs_35_to_80_words():
    def problems(n):
        b = bible()
        b.characters[0].visual_description = " ".join(["word"] * n)
        return [p for p in story.validate_bible(b, ["female lead"], []) if "visual_description" in p]

    assert problems(34) and "35 to 80" in problems(34)[0]
    assert problems(35) == [] and problems(80) == []
    assert problems(81) and "35 to 80" in problems(81)[0]
    assert "35 to 80 words" in story.BIBLE_INSTRUCTIONS
