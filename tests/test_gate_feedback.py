"""Audit: what a person types at each gate reaches the model that redoes the work."""
import uuid
from types import SimpleNamespace

from helpers import at_gate, drive
from test_phase5 import brief, client, env, gate, runner, start, to_gate  # noqa: F401 (fixtures)

from app import db
from app.jobs.handlers import HANDLERS, llm_story_check
from app.vendors import llm


def watch(monkeypatch):
    """Record (role, system, messages text) of every language-model call, then run it as usual."""
    seen = []
    real = llm.complete

    def spy(role, system, messages, *a, **k):
        seen.append(SimpleNamespace(role=role, system=system, text=" ".join(str(m["content"]) for m in messages)))
        return real(role, system, messages, *a, **k)

    monkeypatch.setattr(llm, "complete", spy)
    return seen


def test_gate_1_the_edited_story_is_what_the_story_check_reads(runner, monkeypatch):
    calls = []

    def check(job):
        calls.append(1)
        if len(calls) == 1:
            return {"has_named_leads": True, "has_conflict": False, "has_beginning_middle_end": True}
        return llm_story_check.run(job)

    HANDLERS["llm_story_check"] = SimpleNamespace(run=check)
    c = client()
    data, files = brief(data={"genre_id": "", "custom_genre": "Dark fantasy", "story_text": "word " * 1600})
    sid = uuid.UUID(c.post("/productions", data=data, files=files, headers={"accept": "application/json"}).headers["location"].rsplit("/", 1)[1])
    drive(runner, sid, at_gate(1))
    seen = watch(monkeypatch)
    assert gate(c, sid, 1, decision="approve", story_text="word " * 1700 + " ZEPHYR_CONFLICT_MARKER").status_code == 303
    drive(runner, sid, at_gate(2))
    assert any(x.role == "writer" and "ZEPHYR_CONFLICT_MARKER" in x.text for x in seen)


def test_gate_2_rejection_notes_reach_the_bible_the_beats_and_the_scripts(runner, monkeypatch):
    c = client()
    sid = start(c, data={"language": "en"})
    to_gate(runner, c, sid, 2)
    seen = watch(monkeypatch)
    for target, role, note in (("bible", "story", "make the lead a night-shift nurse"), ("beats", "story", "slow the first reveal to episode 3"), ("scripts", "writer", "shorter, sharper lines")):
        seen.clear()
        assert gate(c, sid, 2, decision="reject", target=target, notes=note).status_code == 303
        drive(runner, sid, at_gate(2))
        hits = [x for x in seen if x.role == role and f"Reviewer notes (apply these):\n{note}" in x.text]
        assert hits, f"the {target} note never reached a {role} call"
