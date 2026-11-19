"""Phase 10: decisions and money (guide 7.5, 12.5 to 12.8): feedback by timestamp, budget approval, alerts, event stream, pages."""
import asyncio
import json
import time
import uuid
from decimal import Decimal

import pytest
from helpers import at_gate, drive
from psycopg.types.json import Jsonb
from test_phase5 import client, env, gate, jobs_of, runner, start, to_gate  # noqa: F401 (fixtures)

from app import alerts, budget, db, decisions, events, feedback, overview
from app.jobs import ledger
from app.jobs.handlers import qa_handlers as Q
from app.models.enums import AlertKind, JobKind

# ---------------------------------------------------------------- scaffolding (no graph)

BOUNDS = [(0, 6), (6, 14), (14, 17), (17, 20), (20, 30)]     # shot time ranges of the scaffolded episode, seconds


def user_id(email="owner@x.io"):
    return db.fetch_one("select id from users where email = %s", (email,))["id"]


def scaffold(sid, number=5, bounds=BOUNDS, compiled=True):
    ep = db.fetch_one("insert into episodes (series_id, number, batch, status, beat, runtime_seconds, cut_version, final_r2_key) values (%s,%s,'pilot','compiled','{}',%s,2,%s) returning *",
                      (sid, number, bounds[-1][1], f"series/{sid}/episodes/{number:02d}/final_v2.mp4" if compiled else None))
    shots = []
    for i, (a, b) in enumerate(bounds):
        s = db.fetch_one("insert into shots (series_id, episode_id, idx, shot_type, tier, spec, status, planned_duration) values (%s,%s,%s,'non_speaking','A','{}','qa_passed',%s) returning id",
                         (sid, ep["id"], i, b - a))
        db.execute("insert into edit_entries (episode_id, cut_version, shot_id, start_seconds, end_seconds) values (%s,2,%s,%s,%s)", (ep["id"], s["id"], a, b))
        db.execute("insert into edit_entries (episode_id, cut_version, shot_id, start_seconds, end_seconds) values (%s,1,%s,0,%s)", (ep["id"], s["id"], bounds[-1][1]))   # an older cut that must be ignored
        shots.append(s["id"])
    return ep, shots


def at(sid, stage, status="waiting_gate"):
    db.execute("update series set status = %s, current_stage = %s where id = %s", (status, stage, sid))


# ---------------------------------------------------------------- feedback: parsing, validation, mapping

def test_the_reference_format_is_parsed_exactly():
    assert feedback.parse_reference("Episode 5 (00:14-00:20)") == (5, 14, 20)
    assert feedback.parse_reference("Episode 12 (01:05-02:00)") == (12, 65, 120)
    for bad in ("Episode 5 (0:14-0:20)", "episode 5 (00:14-00:20)", "Episode 5 00:14-00:20", "Episode x (00:14-00:20)", "", "Episode 5 (00:14-00:99)"):
        with pytest.raises(feedback.FeedbackError):
            feedback.parse_reference(bad)
    assert feedback.split_entry("Episode 5 (00:14-00:20) her mouth is off") == ("Episode 5 (00:14-00:20)", "her mouth is off")
    with pytest.raises(feedback.FeedbackError):
        feedback.split_entry("no reference at all")


def test_episode_5_at_14_to_20_maps_to_exactly_the_overlapping_shots(env):
    sid = start(client())
    ep, shots = scaffold(sid)
    with db.transaction() as conn:
        item = feedback.create_item(conn, sid, user_id(), "Episode 5 (00:14-00:20)", "the face changes")
    assert item["shot_ids"] == [shots[2], shots[3]]                                  # [14,17) and [17,20); [6,14) only touches at 14 and [20,30) at 20
    with db.transaction() as conn:
        wider = feedback.create_item(conn, sid, user_id(), "Episode 5 (00:13-00:21)", "wider")
    assert wider["shot_ids"] == [shots[1], shots[2], shots[3], shots[4]]            # one second more each side now overlaps their neighbours
    assert db.fetch_one("select count(*) n from jobs where series_id = %s and kind = 'llm_feedback_classify'", (sid,))["n"] == 2


def test_a_reference_must_be_a_compiled_episode_inside_its_runtime(env):
    sid = start(client())
    scaffold(sid)
    scaffold(sid, number=6, compiled=False)
    cases = {"Episode 9 (00:01-00:02)": "not compiled", "Episode 6 (00:01-00:02)": "not compiled", "Episode 5 (00:20-00:14)": "start must be before",
             "Episode 5 (00:10-00:10)": "start must be before", "Episode 5 (00:25-00:31)": "only 30 seconds"}
    for ref, needle in cases.items():
        with pytest.raises(feedback.FeedbackError, match=needle):
            with db.transaction() as conn:
                feedback.create_item(conn, sid, user_id(), ref, "x")
    with pytest.raises(feedback.FeedbackError, match="what is wrong"):
        with db.transaction() as conn:
            feedback.create_item(conn, sid, user_id(), "Episode 5 (00:01-00:02)", "  ")


def test_classification_sets_a_category_and_a_person_can_change_it_when_confirming(env):
    sid = start(client())
    ep, shots = scaffold(sid)
    with db.transaction() as conn:
        item = feedback.create_item(conn, sid, user_id(), "Episode 5 (00:00-00:05)", "her mouth does not match the words")
    job = db.fetch_one("select * from jobs where kind = 'llm_feedback_classify' and series_id = %s", (sid,))
    assert Q.llm_feedback_classify(job)["category"] == "lipsync"
    assert db.fetch_one("select category from feedback_items where id = %s", (item["id"],))["category"] == "lipsync"
    with db.transaction() as conn:
        done = feedback.confirm_item(conn, sid, item["id"], user_id(), "picture")
    assert done["category"] == "picture" and done["status"] == "confirmed"
    with pytest.raises(feedback.FeedbackError, match="Choose a category"):
        with db.transaction() as conn:
            feedback.confirm_item(conn, sid, item["id"], user_id(), "nonsense")
    for note, cat in (("the music is too loud", "music"), ("subtitles are late", "caption"), ("the accent is wrong", "voice"), ("the logo is crooked", "brand"), ("her hand has six fingers", "picture")):
        assert feedback.keyword_category(note) == cat


# ---------------------------------------------------------------- sending feedback and the rework limit

def feedback_items(sid, n=1, category="picture"):
    ids = []
    for i in range(n):
        with db.transaction() as conn:
            item = feedback.create_item(conn, sid, user_id(), f"Episode 5 (00:0{i}-00:0{i + 3})", f"note {i}")
            feedback.confirm_item(conn, sid, item["id"], user_id(), category)
        ids.append(str(item["id"]))
    return ids


def test_a_second_pilot_rework_round_is_refused_but_reassembling_is_not_counted(env):
    sid = start(client())
    scaffold(sid)
    at(sid, "gate4")
    ids = feedback_items(sid)
    with db.transaction() as conn:
        payload = decisions.decide_feedback(conn, sid, user_id())
    assert payload["decision"] == "feedback" and payload["feedback_ids"] == ids and payload["gate"] == 4
    assert db.fetch_one("select status from series where id = %s", (sid,))["status"] == "running"
    cmd = db.fetch_one("select * from graph_commands where series_id = %s and kind = 'resume' order by created_at desc limit 1", (sid,))
    assert cmd["payload"]["feedback_ids"] == ids
    assert db.fetch_one("select count(*) n from alerts where series_id = %s and kind = 'action_started' and message like 'Rework started%%'", (sid,))["n"] == 1
    at(sid, "gate4")                                                                                             # the pilot comes back to Gate 4
    feedback_items(sid)
    with pytest.raises(decisions.DecisionError, match="rework rounds are used up"):
        with db.transaction() as conn:
            decisions.decide_feedback(conn, sid, user_id())
    with db.transaction() as conn:                                                                               # a recompile is not a rework round
        decisions.decide_feedback(conn, sid, user_id(), reassemble=[5])
    assert db.fetch_one("select notes from gate_decisions where series_id = %s and gate = 4 order by round desc limit 1", (sid,))["notes"] == "reassemble"
    at(sid, "gate4")
    with pytest.raises(decisions.DecisionError, match="rework rounds are used up"):
        with db.transaction() as conn:
            decisions.decide_feedback(conn, sid, user_id())
    db.execute("update config set value = '2'::jsonb where key = 'pilot_rework_rounds'")
    from app.config import clear_config_cache

    clear_config_cache()
    with db.transaction() as conn:                                                                               # the limit is a config value
        assert decisions.decide_feedback(conn, sid, user_id())["decision"] == "feedback"


def test_feedback_is_refused_when_nothing_is_confirmed_or_no_gate_is_open(env):
    sid = start(client())
    scaffold(sid)
    with pytest.raises(decisions.DecisionError, match="Feedback can only be sent"):
        with db.transaction() as conn:
            decisions.decide_feedback(conn, sid, user_id())
    at(sid, "gate4")
    with pytest.raises(decisions.DecisionError, match="Confirm at least one"):
        with db.transaction() as conn:
            decisions.decide_feedback(conn, sid, user_id())
    with pytest.raises(decisions.DecisionError, match="compiled episodes"):
        with db.transaction() as conn:
            decisions.decide_feedback(conn, sid, user_id(), reassemble=[77])


def test_the_feedback_routes_and_who_may_use_them(env):
    c, other = client(), client("other@x.io")
    sid = start(c)
    scaffold(sid)
    at(sid, "gate4")
    r = c.post(f"/productions/{sid}/feedback", data={"entry": "Episode 5 (00:14-00:20) the face changes"})
    assert r.status_code == 303
    item = db.fetch_one("select * from feedback_items where series_id = %s", (sid,))
    assert item["note"] == "the face changes" and len(item["shot_ids"]) == 2 and item["status"] == "received"
    assert other.post(f"/productions/{sid}/feedback", data={"entry": "Episode 5 (00:14-00:20) x"}).status_code == 403
    assert c.post(f"/productions/{sid}/feedback", data={"entry": "Episode 5 (00:14-00:20)"}).status_code == 400        # no note
    assert c.post(f"/productions/{sid}/feedback", data={"entry": "garbage"}).status_code == 400
    assert c.post(f"/productions/{sid}/feedback/send").status_code == 400                                              # nothing confirmed
    assert other.post(f"/productions/{sid}/feedback/{item['id']}/confirm", data={"category": "picture"}).status_code == 403
    assert c.post(f"/productions/{sid}/feedback/{item['id']}/confirm", data={"category": "bogus"}).status_code == 400
    assert c.post(f"/productions/{sid}/feedback/{item['id']}/confirm", data={"category": "picture"}).status_code == 303
    assert c.post(f"/productions/{sid}/feedback/send").status_code == 303
    assert c.post(f"/productions/{sid}/feedback/send").status_code == 409                                              # the gate is already decided
    assert c.post(f"/productions/{sid}/reassemble", data={}).status_code == 400
    at(sid, "gate5")
    assert c.post(f"/productions/{sid}/reassemble", data={"episodes": "5"}).status_code == 303
    assert db.fetch_one("select count(*) n from feedback_items where series_id = %s and note = 'Reassemble' and category = 'caption'", (sid,))["n"] == 1


# ---------------------------------------------------------------- rework in the graph, by category

def shot_at(sid, number, predicate):
    rows = db.fetch_all("select s.*, e1.start_seconds a, e1.end_seconds b, cardinality(s.line_ids) line_count from shots s join episodes e on e.id = s.episode_id"
                        " join edit_entries e1 on e1.shot_id = s.id and e1.cut_version = e.cut_version where s.series_id = %s and e.number = %s order by s.idx", (sid, number))
    return next(r for r in rows if predicate(r))


def mmss(t):
    t = int(t)
    return f"{t // 60:02d}:{t % 60:02d}"


def test_each_feedback_category_does_its_own_rework(runner):
    c = client()
    sid = start(c)
    to_gate(runner, c, sid, 4)
    pic = shot_at(sid, 1, lambda s: s["tier"] == "A")
    lip = shot_at(sid, 1, lambda s: s["tier"] == "B2")
    voi = shot_at(sid, 2, lambda s: s["line_count"] > 0)
    sto = shot_at(sid, 2, lambda s: s["tier"] == "A")
    other = shot_at(sid, 3, lambda s: s["idx"] == 0)
    plans = [(1, pic, "picture"), (1, lip, "lipsync"), (2, voi, "voice"), (2, sto, "story"), (3, other, "music"), (3, shot_at(sid, 3, lambda s: s["idx"] == 2), "caption")]
    before = {}
    for number, s, cat in plans:
        r = c.post(f"/productions/{sid}/feedback", data={"entry": f"Episode {number} ({mmss(s['a'])}-{mmss(float(s['a']) + 1)}) note about {cat}"})
        assert r.status_code == 303, r.text
        item = db.fetch_one("select id from feedback_items where series_id = %s and note like %s", (sid, f"%{cat}"))
        assert c.post(f"/productions/{sid}/feedback/{item['id']}/confirm", data={"category": cat}).status_code == 303
        before[cat] = {"cuts": db.fetch_one("select cut_version from episodes where series_id = %s and number = %s", (sid, number))["cut_version"]}
    line = db.fetch_one("select id, version, audio_tag from dialogue_lines where id = %s", (voi["line_ids"][0],))
    music_key = db.fetch_one("select music_r2_key from episodes where series_id = %s and number = 3", (sid,))["music_r2_key"]
    jobs_before = {k: db.fetch_one("select count(*) n from jobs where series_id = %s and kind = %s", (sid, k))["n"] for k in ("image_keyframe", "video_clip", "lipsync", "tts_line", "music_episode", "ffmpeg_compile")}

    assert c.post(f"/productions/{sid}/feedback/send").status_code == 303
    drive(runner, sid, at_gate(4))                                                                                  # the pilot comes back to Gate 4
    count = lambda k: db.fetch_one("select count(*) n from jobs where series_id = %s and kind = %s", (sid, k))["n"]

    assert db.fetch_one("select attempts, spec->>'feedback_id' fb from shots where id = %s", (pic["id"],))["fb"] is not None
    assert len(jobs_of(sid, "image_keyframe")) > jobs_before["image_keyframe"]                                        # picture: drawn again (a new ledger key: the feedback id)
    assert db.fetch_one("select count(*) n from jobs where shot_id = %s and kind = 'video_clip'", (pic["id"],))["n"] == 2
    assert db.fetch_one("select count(*) n from jobs where shot_id = %s and kind = 'lipsync'", (lip["id"],))["n"] == 2   # lipsync on B2: only the lip-sync again
    assert db.fetch_one("select count(*) n from jobs where shot_id = %s and kind = 'video_clip'", (lip["id"],))["n"] == 1
    assert db.fetch_one("select count(*) n from jobs where shot_id = %s and kind = 'image_keyframe'", (lip["id"],))["n"] <= 1
    new = db.fetch_one("select version, audio_tag, audio_r2_key, duration_seconds from dialogue_lines where id = %s", (line["id"],))
    assert new["version"] == line["version"] + 1 and new["audio_r2_key"] and new["duration_seconds"] and new["audio_tag"]   # voice: new version, voiced again
    assert count("tts_line") > jobs_before["tts_line"]
    assert db.fetch_one("select audit_log.id from audit_log where series_id = %s and action = 'note:voice_note:2'", (sid,))                               # the note reaches the audio-tag prompt
    assert db.fetch_one("select music_r2_key from episodes where series_id = %s and number = 3", (sid,))["music_r2_key"] and count("music_episode") == jobs_before["music_episode"] + 1
    items = {r["category"]: r for r in db.fetch_all("select * from feedback_items where series_id = %s", (sid,))}
    assert items["story"]["status"] == "declined" and items["story"]["decline_reason"] == feedback.STORY_DECLINE
    assert all(items[k]["status"] == "fixed" for k in ("picture", "lipsync", "voice", "music", "caption"))              # QA passed again
    assert db.fetch_one("select count(*) n from alerts where series_id = %s and kind = 'feedback_status' and message like '%%fixed%%'", (sid,))["n"] >= 5
    for cat, number in (("picture", 1), ("voice", 2), ("music", 3)):
        assert db.fetch_one("select cut_version from episodes where series_id = %s and number = %s", (sid, number))["cut_version"] > before[cat]["cuts"]
    s = db.fetch_one("select status, current_stage from series where id = %s", (sid,))
    assert (s["status"], s["current_stage"]) == ("waiting_gate", "gate4")
    page = c.get(f"/productions/{sid}/review/episodes").text
    assert "The pilot rework round has been used" in page and "Mark range" not in page                                # a second round is not offered
    assert c.post(f"/productions/{sid}/feedback", data={"entry": "Episode 1 (00:01-00:02) again"}).status_code == 303   # (adding is allowed, sending is not)
    item = db.fetch_one("select id from feedback_items where series_id = %s and note = 'again'", (sid,))
    c.post(f"/productions/{sid}/feedback/{item['id']}/confirm", data={"category": "picture"})
    assert c.post(f"/productions/{sid}/feedback/send").status_code == 400


# ---------------------------------------------------------------- money

def held_series(extra_batch="pilot"):
    c = client()
    sid = start(c)
    with db.transaction() as conn:
        budget.create_budget(conn, sid, extra_batch, 10)
        conn.execute("update series set status = 'held_budget' where id = %s", (sid,))
        conn.execute("update budgets set spent_usd = 12.4 where series_id = %s", (sid,))
        conn.execute("insert into audit_log (series_id, action, detail) values (%s, 'budget.hold', %s)", (sid, Jsonb({"previous_status": "waiting_jobs", "batch": extra_batch})))
    return c, sid


def test_approving_an_amount_releases_a_held_series_and_declining_leaves_it_held(env):
    c, sid = held_series()
    other = client("other@x.io")
    assert other.post(f"/productions/{sid}/budget", data={"decision": "approve", "amount": "5"}).status_code == 403
    for bad in ("", "abc", "0", "-3"):
        assert c.post(f"/productions/{sid}/budget", data={"decision": "approve", "amount": bad}).status_code == 400
    assert c.post(f"/productions/{sid}/budget", data={"decision": "decline"}).status_code == 303
    assert db.fetch_one("select status from series where id = %s", (sid,))["status"] == "held_budget"
    assert db.fetch_one("select count(*) n from audit_log where series_id = %s and action = 'budget.decline'", (sid,))["n"] == 1
    page = c.get(f"/productions/{sid}").text
    assert "Video spending is paused" in page and 'name="amount"' in page
    assert c.post(f"/productions/{sid}/budget", data={"decision": "approve", "amount": "25.50"}).status_code == 303
    b = db.fetch_one("select approved_extra_usd from budgets where series_id = %s", (sid,))
    assert b["approved_extra_usd"] == Decimal("25.50")
    assert db.fetch_one("select status from series where id = %s", (sid,))["status"] == "waiting_jobs"                  # back to the status it had
    assert "Video spending is paused" not in c.get(f"/productions/{sid}").text


def test_the_spend_bar_has_marks_at_100_and_125_percent(env):
    c, sid = held_series()
    sp = overview.spend(sid)["batches"][0]
    assert sp["budget"] == 10 and sp["spent"] == 12.4 and sp["percent"] == pytest.approx(124.0) and sp["limit"] == 12.5
    assert sp["marks"] == [80.0, 100.0] and sp["bar_percent"] == pytest.approx(99.2)
    page = c.get(f"/productions/{sid}").text
    assert 'title="100%"' in page and 'title="125%"' in page and "spent $12.40 (124%)" in page


def test_the_gate_4_projection_is_pilot_spend_per_second_times_the_remaining_seconds(env):
    sid = start(client())
    with db.transaction() as conn:
        budget.create_budget(conn, sid, "pilot", 100)
        conn.execute("update budgets set spent_usd = 30 where series_id = %s", (sid,))
        for n, batch, runtime in ((1, "pilot", 80), (2, "pilot", 80), (3, "pilot", 80), (4, "full", None), (5, "full", 90)):
            conn.execute("insert into episodes (series_id, number, batch, status, beat, runtime_seconds) values (%s,%s,%s,'outlined','{}',%s)", (sid, n, batch, runtime))
    p = overview.projection(sid)
    assert p["pilot_seconds"] == 240 and p["per_second"] == pytest.approx(0.125) and p["remaining_episodes"] == 2
    assert p["remaining_seconds"] == pytest.approx(82.5 + 90) and p["projected"] == pytest.approx(0.125 * 172.5)     # an unplanned episode counts as 82.5 s
    assert overview.projection(start(client("other@x.io"))) is None


# ---------------------------------------------------------------- alerts and the event stream

def test_alert_routes_list_mark_read_and_show_the_unread_count(env):
    c, other = client(), client("other@x.io")
    sid = start(c)
    with db.transaction() as conn:
        alerts.notify([user_id()], AlertKind.gate_open, "Gate open", f"/productions/{sid}", sid, conn=conn)
    page = c.get("/alerts").text
    assert "Gate open" in page and "Mark read" in page and ">2<" in c.get("/").text.replace(" ", "")                    # the start alert and this one
    mine = db.fetch_one("select id from alerts where message = 'Gate open'")["id"]
    assert other.post(f"/alerts/{mine}/read").status_code == 404                                                      # not somebody else's
    assert c.post(f"/alerts/{mine}/read").status_code == 303
    assert db.fetch_one("select read_at from alerts where id = %s", (mine,))["read_at"] is not None
    assert "Gate open" in c.get("/alerts").text and c.post(f"/alerts/{uuid.uuid4()}/read").status_code == 404


def run_stream(user, series_ids, trigger, want, poll=2.0):
    """Start the stream, run `trigger()` shortly after, and return (events seen until `want` arrived, seconds until it arrived)."""
    async def go():
        gen = events.stream(user, series_ids, poll=poll)
        assert (await gen.__anext__()).startswith(": connected")
        loop = asyncio.get_running_loop()
        t0 = time.monotonic()
        loop.call_later(0.3, lambda: asyncio.ensure_future(asyncio.to_thread(trigger)))
        seen = []
        while True:
            chunk = await asyncio.wait_for(gen.__anext__(), timeout=10)
            if chunk.startswith(":"):
                continue
            seen.append(chunk)
            if want in chunk:
                break
        await gen.aclose()
        return seen, time.monotonic() - t0
    return asyncio.run(go())


def test_a_progress_event_arrives_within_three_seconds_of_a_job_changing_status(env):
    sid = start(client())
    job = ledger.enqueue(JobKind.llm_feedback_classify, series_id=sid, request={"x": 1})
    db.execute("update jobs set status = 'running', locked_by = 'w' where id = %s", (job["id"],))

    def change():
        ledger.succeed(job["id"], {"ok": True})

    seen, elapsed = run_stream(user_id(), [sid], change, "event: progress")
    assert elapsed < 3.0, elapsed
    assert json.loads(seen[-1].split("data: ")[1])["series_id"] == str(sid)


def test_an_alert_event_arrives_within_three_seconds_and_old_alerts_are_not_replayed(env):
    sid = start(client())                                                                                                   # this alert exists before the stream starts

    def notify():
        with db.transaction() as conn:
            alerts.notify([user_id()], AlertKind.qa_done, "QA finished", f"/productions/{sid}", sid, conn=conn)

    seen, elapsed = run_stream(user_id(), [sid], notify, "event: alert")
    assert elapsed < 3.0 and len(seen) == 1 and "QA finished" in seen[0] and "Your production has started" not in seen[0]


def test_the_event_stream_needs_a_signed_in_user(env):
    from fastapi.testclient import TestClient
    from app.web.main import app

    assert TestClient(app, base_url="https://testserver").get("/events").status_code == 401


# ---------------------------------------------------------------- pages

def test_the_dashboard_lists_every_production_and_a_waiting_strip_for_the_people_who_decide(env):
    c, other, admin = client(), client("other@x.io"), client("admin@x.io")
    sid = start(c)
    at(sid, "gate2")
    page = c.get("/").text
    assert "Waiting for you" in page and "Gate 2 story review" in page and f"/productions/{sid}/review/story" in page
    for needle in ("<th>Title</th>", "<th>Category</th>", "<th>Language</th>", "<th>Owner</th>", "<th>Stage</th>", "<th>Status</th>", "<th>Spend</th>", "Olive", "standard", "bn"):
        assert needle in page, needle
    assert "Waiting for you" not in other.get("/").text and "Waiting for you" in admin.get("/").text                    # a member who does not own it is not asked
    assert f"/productions/{sid}" in other.get("/").text                                                                    # but anybody can view the table
    at(sid, "gate4")
    assert f"/productions/{sid}/review/episodes" in c.get("/").text
    c2, sid2 = held_series()
    assert "Budget approval needed" in c2.get("/").text


def test_the_tracker_shows_each_stage_as_done_running_waiting_or_not_started():
    base = {"status": "running", "current_stage": "build"}
    states = {t["key"]: t["state"] for t in overview.tracker(base)}
    assert [states[k] for k in ("gate0", "story", "gate3", "build", "qa", "done")] == ["done", "done", "done", "running", "not started", "not started"]
    assert {t["key"]: t["state"] for t in overview.tracker({"status": "waiting_gate", "current_stage": "gate4"})}["gate4"] == "waiting"
    assert {t["key"]: t["state"] for t in overview.tracker({"status": "held_budget", "current_stage": "build"})}["build"] == "waiting"
    assert {t["key"]: t["state"] for t in overview.tracker({"status": "failed", "current_stage": "story"})}["story"] == "failed"
    assert {t["state"] for t in overview.tracker({"status": "done", "current_stage": "done"})} == {"done"}
    assert [t["key"] for t in overview.tracker(base)] == overview.STAGES


def test_the_production_page_shows_stages_an_episode_grid_spend_and_activity(runner):
    c = client()
    sid = start(c)
    to_gate(runner, c, sid, 4)
    page = c.get(f"/productions/{sid}").text
    assert page.count('data-state="done"') >= 8 and 'data-state="waiting"' in page and "Gate 4 pilot" in page
    assert page.count('class="tile"') == 5 and "10/10 shots ready" in page and "pilot" in page
    assert "Pilot video budget" in page and "Marks at 100% and 125%" in page
    assert "qa shot succeeded" in page and "gate.decide" in page                                                  # the activity feed: jobs and audit rows
    assert "Review the episodes" in page
    assert f'data-production="{sid}"' in page and "EventSource" in page


def test_the_episode_review_page_has_the_player_qa_report_feedback_and_the_projection(runner):
    c, other = client(), client("other@x.io")
    sid = start(c)
    to_gate(runner, c, sid, 4)
    page = c.get(f"/productions/{sid}/review/episodes").text
    for needle in ("<video", "Mark range", "QA report", "Episode 1", "Feedback", "Projection for the full build", "Continue to the full build", "Discard", "Reassemble", "Recompile (no new rendering)",
                   "Episode 5 (00:14-00:20)"):
        assert needle in page, needle
    assert "starts[n] = video.currentTime" in page and 'mmss(a) + "-" + mmss(b)' in page                                 # Mark range: first press = start, second = end, fills the form
    view = other.get(f"/productions/{sid}/review/episodes").text
    assert "Mark range" not in view and "Continue to the full build" not in view and "<video" in view                    # a non-owner can watch only
    assert db.fetch_one("select count(*) n from qa_results where series_id = %s and check_name = 'lipsync_review'", (sid,))["n"] >= 3
    assert "no lip-sync scorer, please judge by eye" in page                                                              # unscored shots are listed for the reviewer


def test_the_dashboard_and_pages_need_a_sign_in(env):
    from fastapi.testclient import TestClient
    from app.web.main import app

    anon = TestClient(app, base_url="https://testserver", follow_redirects=False)
    assert anon.get("/", headers={"accept": "text/html"}).status_code == 303 and anon.get("/alerts").status_code == 401
