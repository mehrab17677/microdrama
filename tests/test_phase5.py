import hashlib
import io
import json
import threading
import uuid

import pytest
from fastapi.testclient import TestClient
from helpers import at_gate, drive, is_status, run_jobs, snapshot, step
from PIL import Image

from app import db
from app.config import clear_config_cache
from app.graphs import common, production, work
from app.graphs.runner import GraphRunner
from app.graphs.steps import enqueue_step
from app.jobs import worker
from app.jobs.errors import PermanentError
from app.jobs.handlers import HANDLERS, load_builtin
from app.qa import face
from app.web import auth
from app.web.main import app
from scripts import migrate, seed_config

PW = "correct horse"
ENV = {}


@pytest.fixture()
def env(db_url, monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_TMP_DIR", str(tmp_path))
    migrate.apply_migrations(db_url)
    with db.connect(db_url) as conn:
        conn.execute("truncate users, audit_log, alerts, series, jobs, cost_log, graph_commands, config, genres, sources, camera_setups, learned_rules cascade")
        conn.commit()
    seed_config.seed(db_url)
    db.execute("update config set value = '5'::jsonb where key = 'dryrun_episode_count'")   # 3 pilot + 2 full episodes
    db.execute("update config set value = '[1, 50]'::jsonb where key = 'episode_count_range'")  # the real minimum is 45
    clear_config_cache()
    monkeypatch.setattr(worker, "_sleep", lambda s: None)
    HANDLERS.clear()
    load_builtin()
    monkeypatch.setattr(face, "count_faces", lambda data: 1)
    monkeypatch.setattr(face, "face_vector", lambda data: face.stub_vector("ref:" + hashlib.sha256(data).hexdigest()[:8]))
    for email, name, role in (("owner@x.io", "Olive", "member"), ("admin@x.io", "Ada", "admin"), ("other@x.io", "Omar", "member")):
        db.execute("insert into users (email, display_name, password_hash, role, status) values (%s,%s,%s,%s,'active')", (email, name, auth.hash_password(PW), role))
    genre = db.fetch_one("insert into genres (name, name_normalized) values ('Revenge romance', 'revenge romance') returning id")["id"]
    db.execute("insert into sources (genre_id, batch_id, file_name, file_sha256, r2_key, status, lore_version) values (%s, %s, 'a.txt', 'sha-a', 'sources/a', 'ready', 7)", (genre, uuid.uuid4()))
    ENV['genre'] = genre
    yield
    clear_config_cache()


@pytest.fixture()
def runner(env):
    r = GraphRunner()
    yield r
    r.close()


def client(email="owner@x.io"):
    c = TestClient(app, base_url="https://testserver", follow_redirects=False)
    assert c.post("/auth/login", data={"email": email, "password": PW}).status_code == 303
    return c


def png(size=(600, 800), color=(120, 90, 60)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def brief(**over):
    data = {"category": "standard", "genre_id": str(ENV["genre"]), "keywords": "revenge, small town, slow burn", "language": "bn",
            "char_roles": ["female lead"], "aesthetic_note": "warm amber light, muted teal palette, 1990s wardrobe, soft film grain, long shadows indoors, worn wooden furniture, rainy evenings, quiet streets and faded posters"}
    files = [("char_files", ("lead.png", png(), "image/png"))]
    data.update(over.pop("data", {}))
    files = over.pop("files", files)
    return data, files


def start(c, **over) -> uuid.UUID:
    data, files = brief(**over)
    r = c.post("/productions", data=data, files=files, headers={"accept": "application/json"})
    assert r.status_code == 303, r.text
    return uuid.UUID(r.headers["location"].rsplit("/", 1)[1])


def gate(c, sid, n, **fields):
    return c.post(f"/productions/{sid}/gate/{n}", data=fields)


def voices(sid) -> dict:
    return {r["code"]: f"voice-{r['code']}" for r in db.fetch_all("select code from characters where series_id = %s and voice_profile is not null", (sid,))}


def alerts_of(kind):
    return db.fetch_all("select * from alerts where kind = %s", (kind,))


def test_dry_run_production_goes_from_start_to_done_with_every_gate(runner):
    c = client()
    sid = start(c)
    assert db.fetch_one("select status, current_stage from series where id = %s", (sid,)) == {"status": "running", "current_stage": "story"}

    drive(runner, sid, at_gate(2))
    assert len(alerts_of("gate_open")) == 2                                      # owner and admin
    assert gate(c, sid, 2, decision="approve").status_code == 303

    drive(runner, sid, at_gate(3))
    assert db.fetch_one("select count(*) n from character_assets where series_id = %s and kind = 'master'", (sid,))["n"] == 2
    assert gate(c, sid, 3, decision="approve", voices=json.dumps(voices(sid))).status_code == 303

    drive(runner, sid, at_gate(4))
    eps = {r["number"]: r["status"] for r in db.fetch_all("select number, status from episodes where series_id = %s", (sid,))}
    assert [eps[i] for i in (1, 2, 3)] == ["qa_passed"] * 3                       # the pilot is finished and reviewed first...
    assert [eps[i] for i in (4, 5)] == ["scripted"] * 2                           # ...nothing is generated for the rest yet
    assert db.fetch_one("select count(*) n from shots s join episodes e on e.id = s.episode_id where e.series_id = %s and e.number > 3", (sid,))["n"] == 0
    assert db.fetch_one("select count(*) n from alerts where kind = 'qa_done'")["n"] == 1
    assert c.post(f"/productions/{sid}/pilot", data={"decision": "continue"}).status_code == 303

    drive(runner, sid, at_gate(5))
    assert {r["status"] for r in db.fetch_all("select status from episodes where series_id = %s", (sid,))} == {"qa_passed"}
    assert gate(c, sid, 5, decision="approve").status_code == 303

    s = drive(runner, sid, is_status("done"))
    assert s["current_stage"] == "done" and s["waiting_group"] is None
    assert db.fetch_one("select count(*) n from jobs where series_id = %s and status <> 'succeeded'", (sid,))["n"] == 0
    assert db.fetch_one("select max(attempt) m from jobs where series_id = %s", (sid,))["m"] == 1
    assert [r["gate"] for r in db.fetch_all("select gate from gate_decisions where series_id = %s order by gate", (sid,))] == [0, 1, 2, 3, 4, 5]
    assert {r["batch"] for r in db.fetch_all("select batch from budgets where series_id = %s", (sid,))} == {"pilot", "full"}
    assert db.fetch_one("select count(*) n from alerts where kind = 'action_finished' and series_id = %s", (sid,))["n"] == 1
    from app.vendors import storage
    assert storage.presign_get(f"series/{sid}/delivery/master.mp4").startswith("file://")
    assert {r["status"] for r in db.fetch_all("select status from episodes where series_id = %s", (sid,))} == {"approved"}


# ---------------------------------------------------------------- helpers

def to_gate(runner, c, sid, n):
    """Drive a fresh production to gate n (2, 3, 4 or 5), approving the gates before it."""
    drive(runner, sid, at_gate(2))
    if n == 2:
        return
    assert gate(c, sid, 2, decision="approve").status_code == 303
    drive(runner, sid, at_gate(3))
    if n == 3:
        return
    assert gate(c, sid, 3, decision="approve", voices=json.dumps(voices(sid))).status_code == 303
    drive(runner, sid, at_gate(4))
    if n == 4:
        return
    assert c.post(f"/productions/{sid}/pilot", data={"decision": "continue"}).status_code == 303
    drive(runner, sid, at_gate(5))


def jobs_of(sid, kind):
    return db.fetch_all("select * from jobs where series_id = %s and kind = %s order by created_at", (sid, kind))


# ---------------------------------------------------------------- gate decisions

def test_deciding_a_gate_twice_returns_409(runner):
    c = client()
    sid = start(c)
    drive(runner, sid, at_gate(2))
    assert gate(c, sid, 2, decision="approve").status_code == 303
    again = gate(c, sid, 2, decision="approve")
    assert again.status_code == 409 and "already decided" in again.text
    assert gate(c, sid, 3, decision="approve").status_code == 409                   # not at gate 3 yet
    assert db.fetch_one("select count(*) n from graph_commands where series_id = %s and kind = 'resume' and payload->>'type' = 'gate'", (sid,))["n"] == 1


def test_two_people_deciding_at_once_get_exactly_one_decision(runner):
    sid = start(client())
    drive(runner, sid, at_gate(2))
    results = []
    barrier = threading.Barrier(2)

    def go(email):
        c = client(email)
        barrier.wait()
        results.append(gate(c, sid, 2, decision="approve").status_code)

    ts = [threading.Thread(target=go, args=(e,)) for e in ("owner@x.io", "admin@x.io")]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(results) == [303, 409]
    assert db.fetch_one("select count(*) n from gate_decisions where series_id = %s and gate = 2", (sid,))["n"] == 1
    assert db.fetch_one("select count(*) n from graph_commands where series_id = %s and kind = 'resume' and payload->>'type' = 'gate'", (sid,))["n"] == 1


def test_only_the_owner_or_admin_can_decide(runner):
    sid = start(client())
    drive(runner, sid, at_gate(2))
    assert gate(client("other@x.io"), sid, 2, decision="approve").status_code == 403
    assert db.fetch_one("select status from series where id = %s", (sid,))["status"] == "waiting_gate"
    assert gate(client("admin@x.io"), sid, 2, decision="approve").status_code == 303


def test_gate_rules(runner):
    c = client()
    sid = start(c)
    drive(runner, sid, at_gate(2))
    assert gate(c, sid, 2, decision="reject", target="scripts").status_code == 400                    # notes needed
    assert gate(c, sid, 2, decision="reject", notes="tighter").status_code == 400                      # target needed
    assert gate(c, sid, 2, decision="reject", notes="tighter", target="music").status_code == 400
    assert gate(c, sid, 2, decision="maybe").status_code == 400
    assert db.fetch_one("select status from series where id = %s", (sid,))["status"] == "waiting_gate"   # nothing was consumed
    assert gate(c, sid, 2, decision="approve").status_code == 303
    drive(runner, sid, at_gate(3))
    r = gate(c, sid, 3, decision="approve")                                                             # no voices chosen
    assert r.status_code == 400 and "CH01" in r.text
    assert gate(c, sid, 3, decision="approve", voices="not json").status_code == 400
    partial = {k: v for k, v in list(voices(sid).items())[:1]}
    assert len(voices(sid)) >= 2 and gate(c, sid, 3, decision="approve", voices=json.dumps(partial)).status_code == 400
    assert gate(c, sid, 3, decision="reject", target="").status_code == 400
    assert gate(c, sid, 3, decision="approve", voices=json.dumps(voices(sid))).status_code == 303
    drive(runner, sid, at_gate(4))
    assert c.post(f"/productions/{sid}/pilot", data={"decision": "discard"}).status_code == 400            # needs notes and a category
    assert c.post(f"/productions/{sid}/pilot", data={"decision": "discard", "notes": "no", "category": "mood"}).status_code == 400
    assert c.post(f"/productions/{sid}/pilot", data={"decision": "later"}).status_code == 400
    assert db.fetch_one("select status from series where id = %s", (sid,))["status"] == "waiting_gate"


def test_gate4_discard_cancels_queued_jobs_and_ends_the_run(runner):
    from app.jobs import ledger

    c = client()
    sid = start(c)
    to_gate(runner, c, sid, 4)
    db.execute("update config set value = value || '{\"ghost\": 0}'::jsonb where key = 'vendor_concurrency'")   # a paused vendor: its job stays queued
    clear_config_cache()
    ledger.enqueue("qa_shot", series_id=sid, vendor="ghost", request={"leftover": 1})
    assert c.post(f"/productions/{sid}/pilot", data={"decision": "discard", "notes": "faces drift", "category": "quality"}).status_code == 303
    s = drive(runner, sid, is_status("discarded"))
    assert (s["discard_category"], s["discard_notes"]) == ("quality", "faces drift")
    assert db.fetch_one("select count(*) n from jobs where series_id = %s and status in ('queued','running','submitted')", (sid,))["n"] == 0
    assert db.fetch_one("select status from jobs where series_id = %s and request->>'leftover' = '1'", (sid,))["status"] == "cancelled"
    assert runner.graph.get_state({"configurable": {"thread_id": str(sid)}}).next == ()                        # the graph ended
    assert db.fetch_one("select count(*) n from shots s join episodes e on e.id = s.episode_id where e.series_id = %s and e.number > 3", (sid,))["n"] == 0
    assert step(runner) == 0                                                                                   # nothing more can be spent


# ---------------------------------------------------------------- idempotency and restart

def test_no_idempotency_key_is_executed_twice(runner, monkeypatch):
    from collections import Counter

    seen, real = Counter(), worker.process
    monkeypatch.setattr(worker, "process", lambda job: (seen.update([str(job["id"])]), real(job))[1])
    c = client()
    sid = start(c)
    to_gate(runner, c, sid, 4)
    async_kinds = {r["kind"] for r in db.fetch_all("select distinct kind from jobs where id::text = any(%s)", ([k for k, n in seen.items() if n > 1],))}
    assert async_kinds <= {"video_clip", "lipsync"}                         # submit then finalize; everything else ran once
    assert max(seen.values()) <= 2
    assert db.fetch_one("select count(*) - count(distinct idempotency_key) d from jobs")["d"] == 0
    # running a node again enqueues nothing new
    before = db.fetch_one("select count(*) n from jobs where series_id = %s", (sid,))["n"]
    for name, fn in (("bible", work.bible_items), ("scripts", work.scripts_items), ("story_checks", work.story_check_items), ("masters", work.masters_items), ("packs", work.pack_items)):
        outcome, exhausted = enqueue_step(sid, name, "pilot", 0, fn(sid, "pilot", 0))
        assert (outcome, exhausted) == ("idle", [])
    assert db.fetch_one("select count(*) n from jobs where series_id = %s", (sid,))["n"] == before


class Crash(BaseException):
    """Stands in for the process being killed: it is not an Exception, so nothing handles it."""


def test_killing_and_restarting_the_runner_mid_run_loses_nothing(env, monkeypatch):
    c = client()
    sid = start(c)
    real, crashed = production.validate_beats, []

    def dying(state):
        if not crashed:
            crashed.append(1)
            raise Crash()
        return real(state)

    monkeypatch.setattr(production, "validate_beats", dying)
    first = GraphRunner()
    with pytest.raises(Crash):
        drive(first, sid, at_gate(2))
    first.close()                                                          # the process is gone
    assert db.fetch_one("select status from graph_commands where kind = 'resume' order by created_at desc limit 1")["status"] == "running"
    done_before = db.fetch_one("select count(*) n from jobs where series_id = %s and status = 'succeeded'", (sid,))["n"]
    assert done_before >= 2
    monkeypatch.setattr(production, "validate_beats", real)
    second = GraphRunner()                                                 # a new process picks it up
    try:
        drive(second, sid, at_gate(2))
    finally:
        second.close()
    assert len(jobs_of(sid, "llm_bible")) == 1 and len(jobs_of(sid, "llm_beats")) == 1 and len(jobs_of(sid, "llm_script")) == 5
    assert db.fetch_one("select count(*) n from jobs where series_id = %s and status <> 'succeeded'", (sid,))["n"] == 0
    assert db.fetch_one("select count(*) n from graph_commands where status = 'running'")["n"] == 0


def test_a_stale_resume_is_ignored_until_the_steps_own_group_finishes(runner):
    c = client()
    sid = start(c)
    runner.process_commands()                                              # graph runs to its first wait (bible)
    s = db.fetch_one("select status, waiting_group from series where id = %s", (sid,))
    assert s["status"] == "waiting_jobs" and ":bible:" in s["waiting_group"]
    db.execute("insert into graph_commands (series_id, graph, thread_id, kind, payload) values (%s, 'production', %s, 'resume', %s::jsonb)",
               (sid, str(sid), json.dumps({"type": "jobs_done", "group": "some-old-group", "failed": 0})))
    runner.process_commands()
    after = db.fetch_one("select status, waiting_group from series where id = %s", (sid,))
    assert dict(after) == dict(s) and jobs_of(sid, "llm_beats") == []     # paused again; the next step did not start
    drive(runner, sid, at_gate(2))                                         # and the real resume still works


# ---------------------------------------------------------------- failures and retries

def stub(kind):
    from app.jobs.handlers import dryrun_stubs

    return dryrun_stubs.HANDLERS[kind]


def test_a_failed_job_is_retried_as_a_new_attempt_in_a_new_group(runner):
    from types import SimpleNamespace

    calls = []

    def flaky(job):
        calls.append(1)
        if len(calls) == 1:
            raise PermanentError("boom")
        return stub("llm_bible").run(job)

    HANDLERS["llm_bible"] = SimpleNamespace(run=flaky)
    c = client()
    sid = start(c)
    drive(runner, sid, at_gate(2))
    jobs = jobs_of(sid, "llm_bible")
    assert [(j["attempt"], j["status"]) for j in jobs] == [(1, "failed"), (2, "succeeded")]
    assert jobs[0]["wait_group"].endswith(":story:pilot:0".replace(":story", ":bible")) and jobs[1]["wait_group"].endswith(":bible:pilot:0:a2")


def test_a_step_that_keeps_failing_stops_the_series_with_an_alert(runner):
    from types import SimpleNamespace

    def broken(job):
        raise PermanentError("always")

    HANDLERS["llm_bible"] = SimpleNamespace(run=broken)
    sid = start(client())
    drive(runner, sid, is_status("failed"))
    assert [j["attempt"] for j in jobs_of(sid, "llm_bible")] == [1, 2, 3]
    assert len(alerts_of("step_failed")) == 2 and "bible" in alerts_of("step_failed")[0]["message"]
    assert runner.graph.get_state({"configurable": {"thread_id": str(sid)}}).next == ()
    assert jobs_of(sid, "llm_beats") == []


def test_a_shot_that_keeps_failing_goes_to_a_human_and_the_batch_carries_on(runner):
    from types import SimpleNamespace

    from app.jobs.handlers import dryrun_stubs

    class Bad:
        finalize = staticmethod(dryrun_stubs._VideoClip.finalize)

        @staticmethod
        def submit(job):
            row = db.fetch_one("select s.idx, e.number from shots s join episodes e on e.id = s.episode_id where s.id = %s", (job["shot_id"],))
            if (row["number"], row["idx"]) == (1, 0):
                raise PermanentError("content policy")
            return dryrun_stubs._VideoClip.submit(job)

    HANDLERS["video_clip"] = Bad
    c = client()
    sid = start(c)
    to_gate(runner, c, sid, 4)
    bad = db.fetch_one("select s.* from shots s join episodes e on e.id = s.episode_id where e.series_id = %s and e.number = 1 and s.idx = 0", (sid,))
    assert bad["status"] == "needs_human"
    tries = db.fetch_all("select attempt, status, wait_group from jobs where shot_id = %s and kind = 'video_clip' order by attempt", (bad["id"],))
    assert [(t["attempt"], t["status"]) for t in tries] == [(1, "failed"), (2, "failed"), (3, "failed")]
    assert tries[1]["wait_group"].endswith(":a2") and tries[2]["wait_group"].endswith(":a3")
    eps = {r["number"]: r["status"] for r in db.fetch_all("select number, status from episodes where series_id = %s", (sid,))}
    assert eps[2] == eps[3] == "qa_passed" and eps[1] != "qa_passed"        # episode 1 waits for a human; the others finish
    assert "1 flagged" in alerts_of("qa_done")[0]["message"]
    assert db.fetch_one("select spent_usd from budgets where series_id = %s and batch = 'pilot'", (sid,))["spent_usd"] > 0


def test_beat_sheet_errors_send_the_step_round_again_twice_at_most(runner):
    from types import SimpleNamespace

    runs = []

    def beats(job):
        out = stub("llm_beats").run(job)
        runs.append(1)
        if len(runs) == 1:
            db.execute("delete from episodes where series_id = %s and number = 3", (job["series_id"],))     # a gap
        return out

    HANDLERS["llm_beats"] = SimpleNamespace(run=beats)
    sid = start(client())
    drive(runner, sid, at_gate(2))
    jobs = jobs_of(sid, "llm_beats")
    assert len(jobs) == 2 and jobs[0]["request"]["errors"] == "" and "no gaps" in jobs[1]["request"]["errors"]
    assert common.latest_note(sid, "beats_errors") == ""                                                   # cleared once it passed

    def always_bad(job):
        stub("llm_beats").run(job)
        db.execute("update episodes set beat = beat - 'hook' where series_id = %s and number = 1", (job["series_id"],))
        return {}

    HANDLERS["llm_beats"] = SimpleNamespace(run=always_bad)
    sid2 = start(client())
    drive(runner, sid2, is_status("failed"))
    assert len(jobs_of(sid2, "llm_beats")) == 3
    assert "kept failing validation" in alerts_of("step_failed")[0]["message"]


def test_a_shot_list_that_breaks_the_rules_is_redone(runner):
    from types import SimpleNamespace

    first = set()

    def shots(job):
        stub("llm_shot_list").run(job)
        if job["episode_id"] not in first:
            first.add(job["episode_id"])
            db.execute("update shots set spec = jsonb_set(spec, '{is_hook}', 'false') where episode_id = %s and idx = 0", (job["episode_id"],))
        return {}

    HANDLERS["llm_shot_list"] = SimpleNamespace(run=shots)
    c = client()
    sid = start(c)
    to_gate(runner, c, sid, 4)
    jobs = jobs_of(sid, "llm_shot_list")
    assert len(jobs) == 6 and sum(1 for j in jobs if "must be the hook" in j["request"]["errors"]) == 3
    assert all(common.count_notes(sid, f"shots_redo:{n}") == 1 for n in (1, 2, 3))
    assert {r["status"] for r in db.fetch_all("select status from episodes where series_id = %s and number <= 3", (sid,))} == {"qa_passed"}


# ---------------------------------------------------------------- story and look rejections

def test_rejecting_the_scripts_redoes_only_the_scripts(runner):
    c = client()
    sid = start(c)
    drive(runner, sid, at_gate(2))
    assert len(jobs_of(sid, "llm_script")) == 5
    assert gate(c, sid, 2, decision="reject", notes="tighter dialogue", target="scripts").status_code == 303
    drive(runner, sid, at_gate(2))
    assert len(jobs_of(sid, "llm_bible")) == 1 and len(jobs_of(sid, "llm_beats")) == 1       # untouched
    scripts = jobs_of(sid, "llm_script")
    assert len(scripts) == 10 and {j["request"]["notes"] for j in scripts[5:]} == {"tighter dialogue"} and {j["request"]["rev"] for j in scripts[5:]} == {1}
    assert len(jobs_of(sid, "llm_continuity")) == 2 and len(jobs_of(sid, "llm_originality")) == 2
    assert [d["decision"] for d in db.fetch_all("select decision from gate_decisions where series_id = %s and gate = 2 order by round", (sid,))] == ["reject"]
    assert gate(c, sid, 2, decision="approve").status_code == 303
    assert db.fetch_one("select count(*) n from gate_decisions where series_id = %s and gate = 2", (sid,))["n"] == 2


def test_rejecting_the_bible_redoes_the_story_from_the_top(runner):
    c = client()
    sid = start(c)
    drive(runner, sid, at_gate(2))
    gate(c, sid, 2, decision="reject", notes="a different cast", target="bible")
    drive(runner, sid, at_gate(2))
    assert len(jobs_of(sid, "llm_bible")) == 2 and len(jobs_of(sid, "llm_beats")) == 2 and len(jobs_of(sid, "llm_script")) == 10
    assert jobs_of(sid, "llm_bible")[1]["request"]["notes"] == "a different cast"
    assert db.fetch_one("select count(*) n from story_bibles where series_id = %s", (sid,))["n"] == 2


def test_rejecting_the_look_redoes_only_the_listed_assets(runner):
    c = client()
    sid = start(c)
    to_gate(runner, c, sid, 3)
    packs_before = len(jobs_of(sid, "image_pack"))
    victims = [r["id"] for r in db.fetch_all("select id from character_assets where series_id = %s and kind = 'turnaround' order by created_at limit 2", (sid,))]
    assert gate(c, sid, 3, decision="reject", notes="faces too old", target=",".join(str(v) for v in victims)).status_code == 303
    drive(runner, sid, at_gate(3))
    assert len(jobs_of(sid, "image_pack")) == packs_before + 2 and len(jobs_of(sid, "image_master")) == 2
    assert db.fetch_one("select count(*) n from character_assets where id = any(%s)", (victims,))["n"] == 0


def test_a_master_too_like_its_reference_is_regenerated_then_the_photo_is_dropped(runner, monkeypatch):
    real = face.stub_vector
    same = lambda seed: real("CH01:1") if seed.startswith("CH01") else real(seed)           # noqa: E731
    monkeypatch.setattr(face, "stub_vector", same)
    monkeypatch.setattr(face, "face_vector", lambda data: real("CH01:1"))                     # the reference photo IS the master
    sid = start(client())
    drive(runner, sid, at_gate(2))
    gate(client(), sid, 2, decision="approve")
    drive(runner, sid, at_gate(3))
    masters = db.fetch_all("select request from jobs where series_id = %s and kind = 'image_master' and request->>'character_id' = (select id::text from characters where series_id = %s and code = 'CH01') order by created_at", (sid, sid))
    assert [(m["request"]["try"], m["request"]["use_photo"]) for m in masters] == [(1, True), (2, True), (3, False)]   # twice at most, then without the photo
    likeness = db.fetch_all("select passed, score from qa_results where series_id = %s and check_name = 'likeness'", (sid,))
    assert len(likeness) == 1 and likeness[0]["passed"] is False                               # shown at Gate 3


def test_a_master_that_differs_passes_first_time(runner):
    sid = start(client())
    drive(runner, sid, at_gate(2))
    gate(client(), sid, 2, decision="approve")
    drive(runner, sid, at_gate(3))
    assert len(jobs_of(sid, "image_master")) == 2
    assert db.fetch_one("select passed from qa_results where series_id = %s and check_name = 'likeness'", (sid,))["passed"] is True


def test_pack_images_that_do_not_match_the_master_are_redone_twice_at_most(runner, monkeypatch):
    from app.jobs.handlers import dryrun_stubs

    real_pack = dryrun_stubs.image_pack

    def pack(job):
        out = real_pack(job)
        if job["request"]["kind"] == "turnaround" and job["request"]["tags"]["angle"] == "back":
            db.execute("update character_assets set face_vector = %s::vector where id = %s", (db.to_vector(face.stub_vector("other")), out["asset_id"]))
        return out

    from types import SimpleNamespace
    HANDLERS["image_pack"] = SimpleNamespace(run=pack)
    c = client()
    sid = start(c)
    drive(runner, sid, at_gate(2))
    gate(c, sid, 2, decision="approve")
    drive(runner, sid, at_gate(3))
    backs = [j for j in jobs_of(sid, "image_pack") if j["request"]["kind"] == "turnaround" and j["request"]["tags"]["angle"] == "back"]
    assert [b["request"]["try"] for b in backs] == [0, 1, 2]                                  # regenerated twice, then flagged
    assert common.count_notes(sid, "pack_retry") == 2


# ---------------------------------------------------------------- a user-supplied story

def test_an_incomplete_story_goes_to_gate1_and_resumes_when_edited(runner):
    from types import SimpleNamespace

    calls = []

    def check(job):
        calls.append(1)
        return {"has_named_leads": True, "has_conflict": len(calls) > 1, "has_beginning_middle_end": True}

    HANDLERS["llm_story_check"] = SimpleNamespace(run=check)
    c = client()
    data, files = brief(data={"genre_id": "", "custom_genre": "Dark fantasy", "story_text": "word " * 1600})
    r = c.post("/productions", data=data, files=files, headers={"accept": "application/json"})
    sid = uuid.UUID(r.headers["location"].rsplit("/", 1)[1])
    drive(runner, sid, at_gate(1))
    assert "a central conflict" in [a for a in alerts_of("gate_open")][0]["message"]
    assert db.fetch_one("select count(*) n from sources where series_id = %s", (sid,))["n"] == 1
    assert gate(c, sid, 1, decision="approve").status_code == 400                                   # the edited story is required
    assert gate(c, sid, 1, decision="approve", story_text="word " * 1700 + " conflict").status_code == 303
    drive(runner, sid, at_gate(2))
    assert len(calls) == 2 and db.fetch_one("select max(version) v from briefs where series_id = %s", (sid,))["v"] == 2
    old = db.fetch_one("select count(*) n from sources where series_id = %s", (sid,))["n"]
    assert old == 1                                                                                 # the old story source was replaced
    assert db.fetch_one("select status from sources where series_id = %s", (sid,))["status"] == "ready"


def test_a_complete_user_story_goes_straight_on(runner):
    c = client()
    data, files = brief(data={"genre_id": "", "custom_genre": "Dark fantasy", "story_text": "word " * 1600})
    sid = uuid.UUID(c.post("/productions", data=data, files=files, headers={"accept": "application/json"}).headers["location"].rsplit("/", 1)[1])
    drive(runner, sid, at_gate(2))
    assert db.fetch_one("select count(*) n from jobs where series_id = %s and kind = 'llm_story_check'", (sid,))["n"] == 1


# ---------------------------------------------------------------- starting a production

def post_json(c, data, files):
    return c.post("/productions", data=data, files=files, headers={"accept": "application/json"})


def test_an_incomplete_brief_is_rejected_with_every_problem_listed(env):
    c = client()
    r = post_json(c, {}, [])
    assert r.status_code == 400
    errors = r.json()["errors"]
    text = " | ".join(errors)
    for needle in ("Choose a category", "No reference material exists for this genre", "3 to 8 keywords", "dialogue language", "at least one character reference", "aesthetic reference images"):
        assert needle in text, needle
    assert len(errors) == 6
    assert db.fetch_one("select count(*) n from series")["n"] == 0 and db.fetch_one("select count(*) n from graph_commands")["n"] == 0


def test_every_intake_rule(env, monkeypatch):
    c = client()

    def errors(data=None, files=None, **kw):
        d, f = brief(data=data or {}, files=files) if files is not None else brief(data=data or {})
        r = post_json(c, d, f)
        return [] if r.status_code == 303 else r.json()["errors"]      # a valid brief just starts the production

    assert any("too small" in e for e in errors(files=[("char_files", ("a.png", png((300, 900)), "image/png"))]))
    assert any("not a readable image" in e for e in errors(files=[("char_files", ("a.png", b"not an image", "image/png"))]))
    assert any("needs a role label" in e for e in errors({"char_roles": [" "]}))
    assert any("at least one character reference" in e for e in errors(files=[]))
    monkeypatch.setattr(face, "count_faces", lambda data: 2)
    assert any("exactly one clear face (found 2)" in e for e in errors())
    monkeypatch.setattr(face, "count_faces", lambda data: 0)
    assert any("(found 0)" in e for e in errors())
    monkeypatch.setattr(face, "count_faces", lambda data: 1)
    assert any("3 to 8 keywords" in e for e in errors({"keywords": "one, two"}))
    assert any("3 to 8 keywords" in e for e in errors({"keywords": ", ".join(f"k{i}" for i in range(9))}))
    assert any("dialogue language" in e for e in errors({"language": "fr"}))
    assert any("aesthetic" in e.lower() for e in errors({"aesthetic_note": "too short"}))
    assert not any("aesthetic" in e.lower() for e in errors({"aesthetic_note": ""}, files=[("char_files", ("a.png", png(), "image/png")), *[("aesthetic_files", (f"{i}.png", png(), "image/png")) for i in range(3)]]))
    empty = db.fetch_one("insert into genres (name, name_normalized) values ('Horror', 'horror') returning id")["id"]
    assert any("no processed sources" in e for e in errors({"genre_id": str(empty)}))
    assert any("no processed sources" in e for e in errors({"genre_id": "not-a-uuid"}))
    assert any("at least 1,500 words (yours has 3)" in e for e in errors({"genre_id": "", "custom_genre": "Fantasy", "story_text": "just three words"}))
    d, f = brief(data={"genre_id": "", "custom_genre": "Fantasy", "story_text": "w " * 1500})
    assert post_json(c, d, f).status_code == 303                                                  # exactly 1,500 words is enough


def test_ad_placement_needs_a_brand_brief_and_assets(env):
    c = client()
    d, f = brief(data={"category": "ad_placement"})
    text = " | ".join(post_json(c, d, f).json()["errors"])
    for needle in ("Brand name", "Product", "placement type", "brand asset"):
        assert needle in text, needle
    svg = b"<svg xmlns='http://www.w3.org/2000/svg'><rect width='4' height='4'/></svg>"
    d, f = brief(data={"category": "ad_placement", "brand_name": "Acme", "product": "Pay app", "placement_types": ["phone_full", "spoken"], "frequency_note": "every 5th episode",
                       "exclusions": "scams, crime", "spoken_wording": "Use Acme Pay", "brand_kinds": ["app_screen"], "brand_labels": ["Home screen"]})
    shot_png = png()
    f.append(("brand_files", ("logo.png", shot_png, "image/png")))
    r = post_json(c, d, f)
    assert r.status_code == 303
    sid = uuid.UUID(r.headers["location"].rsplit("/", 1)[1])
    bb = db.fetch_one("select * from brand_briefs where series_id = %s", (sid,))
    assert (bb["brand_name"], bb["placement_types"], bb["exclusions"], bb["version"]) == ("Acme", ["phone_full", "spoken"], ["scams", "crime"], 1)
    asset = db.fetch_one("select * from brand_assets where series_id = %s", (sid,))
    assert asset["kind"] == "app_screen" and asset["r2_key"].endswith(".png")
    from app.vendors import storage
    import tempfile, pathlib
    out = pathlib.Path(tempfile.mkdtemp()) / "x.png"
    storage.get_to_file(asset["r2_key"], out)
    assert out.read_bytes() == shot_png                                                                  # reproduced exactly as supplied


def test_a_valid_brief_creates_everything_in_one_go(env):
    c = client()
    sid = start(c)
    s = db.fetch_one("select * from series where id = %s", (sid,))
    assert (s["status"], s["current_stage"], s["thread_id"], s["pinned_lore_version"], s["genre_id"], s["language"], s["category"]) == ("running", "story", str(sid), 7, ENV["genre"], "bn", "standard")
    b = db.fetch_one("select * from briefs where series_id = %s", (sid,))
    assert b["version"] == 1 and b["keywords"] == ["revenge", "small town", "slow burn"] and b["story_text"] is None
    refs = db.fetch_all("select kind, role_label, r2_key, face_vector is not null has_vec from brief_refs where series_id = %s", (sid,))
    assert [(r["kind"], r["role_label"], r["has_vec"]) for r in refs] == [("character", "female lead", True)]
    assert refs[0]["r2_key"].startswith(f"series/{sid}/brief/") and refs[0]["r2_key"].endswith(".png")
    assert [g["gate"] for g in db.fetch_all("select gate from gate_decisions where series_id = %s order by gate", (sid,))] == [0, 1]
    cmd = db.fetch_one("select * from graph_commands where series_id = %s", (sid,))
    assert (cmd["kind"], cmd["graph"], cmd["status"], cmd["thread_id"]) == ("start", "production", "queued", str(sid))
    assert db.fetch_one("select 1 from audit_log where series_id = %s and action = 'production.create'", (sid,))
    assert alerts_of("action_started")[0]["series_id"] == sid


# ---------------------------------------------------------------- pages

def test_production_pages(env):
    assert TestClient(app, base_url="https://testserver").get("/productions/new").status_code == 401
    c = client()
    first = c.get("/productions/new")
    assert first.status_code == 200 and "Standard drama" in first.text and "Ad placement drama" in first.text and "Start production" not in first.text
    form = c.get("/productions/new?category=standard")
    assert "Revenge romance" in form.text and "My genre is not listed" in form.text and "No reference material exists" in form.text
    assert "Brand brief" in c.get("/productions/new?category=ad_placement").text
    sid = start(c)
    page = c.get(f"/productions/{sid}")
    assert page.status_code == 200 and "running" in page.text and "story" in page.text
    assert c.get(f"/productions/{uuid.uuid4()}").status_code == 404


# ---------------------------------------------------------------- the pure pieces

def test_pack_plan_follows_the_guide_table():
    from app.stages.look import pack_plan

    lead = pack_plan(True, ["W01", "W02"], ["calm", "anger", "sadness"])
    kinds = [p["kind"] for p in lead]
    assert (kinds.count("turnaround"), kinds.count("expression"), kinds.count("wardrobe"), kinds.count("pose")) == (5, 3, 4, 4)
    assert len(pack_plan(True, [], [f"g{i}" for i in range(12)])) == 5 + 8 + 4                      # expressions capped at 8
    other = pack_plan(False, ["W01", "W02"], ["a", "b", "c", "d"])
    assert [p["kind"] for p in other].count("expression") == 3 and [p["kind"] for p in other].count("wardrobe") == 2 and len(other) == 5


def test_durations_follow_the_guide():
    from app.media import durations as d

    t = {"pad_head": 0.25, "pad_tail": 0.35, "line_gap": 0.30}
    assert d.planned_seconds([2, 2, 2], None, t) == pytest.approx(7.2) and d.planned_seconds([], 12, t) == 10 and d.planned_seconds([], 0.5, t) == 2
    assert d.render_seconds("B2", d.planned_seconds([1.2], None, t)) == 4       # a 1.2 s line renders at the 4 s minimum, then is trimmed
    assert d.render_seconds("C", 1.5) == 3 and d.render_seconds("A", 9.2) == 10 and d.render_seconds("B1", 7.2) == 8
    short = [{"idx": i, "tier": "A", "line_durations": [], "est_seconds": 5.0} for i in range(12)]    # 60 s: too short
    fitted = d.fit_episode(short, t)
    assert fitted["in_range"] and 75 <= fitted["runtime"] <= 90 and all(2 <= p <= 10 for p in fitted["planned"].values())
    long_shot = [{"idx": 0, "tier": "B2", "line_durations": [4, 4, 4], "est_seconds": None}]
    assert d.fit_episode(long_shot, t)["too_long"] == [0]


def test_routing_costs_and_requests():
    from app.media import costs, requests as r, routing

    assert [routing.tier_for(t) for t in ("non_speaking", "speaking_closeup", "speaking_wider", "hero", "static_insert")] == ["A", "B1", "B2", "C", "INSERT"]
    assert routing.needs_lipsync("B2", 1) and routing.needs_lipsync("C", 2) and not routing.needs_lipsync("B1", 3) and not routing.needs_lipsync("B2", 0) and not routing.needs_lipsync("A", 1)
    assert costs.shot_clip_cost("A", 8, 8, 0) > costs.shot_clip_cost("B2", 8, 8, 1) > 0 and costs.shot_clip_cost("INSERT", 4, 4, 0) == 0
    assert costs.shot_lipsync_cost("B2", 3, 8) == pytest.approx(0.14) and costs.shot_lipsync_cost("A", 0, 8) == 0
    shot = {"id": "s1", "keyframe_r2_key": "k.png", "render_duration": 8, "attempts": 0, "line_count": 3}
    req = r.video_request("B2", shot, "p", False)
    assert req["arguments"]["generate_audio"] is False and req["arguments"]["duration"] == "8" and req["arguments"]["aspect_ratio"] == "9:16" and req["arguments"]["image_key"] == "k.png"
    assert r.video_request("A", shot, "p", True)["arguments"]["generate_audio"] is True
    assert r.video_request("C", shot, "p", False)["arguments"]["generate_audio"] is False and "aspect_ratio" not in r.video_request("C", shot, "p", False)["arguments"]
    assert r.stable_seed("s1", 0) != r.stable_seed("s1", 1) and r.stable_seed("s1", 0) == int(hashlib.sha256(b"s10").hexdigest()[:8], 16)


def test_graph_state_has_only_four_keys():
    from app.graphs.state import ProductionState

    assert set(ProductionState.__annotations__) == {"series_id", "batch", "round", "last_resume"}
