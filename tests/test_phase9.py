"""Phase 9: QA, repairs and learning (guide 11)."""
import json
import subprocess
import uuid
from types import SimpleNamespace

import pytest
from test_phase5 import brief, client, env, runner, start, to_gate  # noqa: F401 (fixtures)
from test_phase6 import job_for
from test_phase8 import pilot  # noqa: F401 (fixture: pilot with real compile)

from app import db
from app.jobs.handlers import HANDLERS
from app.jobs.handlers import qa_handlers as Q
from app.jobs.handlers import look_handlers
from app.media import routing
from app.qa import episode as eq, face, lipsync, repair, shot as shot_qa, vision
from app.stages import learning
from app.vendors import falmedia, images, storage

# ---------------------------------------------------------------- helpers

def shot_row(**over):
    base = {"id": uuid.uuid4(), "tier": "B2", "line_count": 2, "fallback_used": False, "sync_attempts": 0, "attempts": 0, "planned_duration": 6.0,
            "spec": {}, "status": "qa_failed", "episode_number": 1, "idx": 1}
    return {**base, **over}


def plan(over, *fails):
    return repair.decide(shot_row(**over), list(fails))


def vq(**over):
    return vision.VisionQA(**{"face_consistent": True, "outfit_correct": True, "action_happens": True, "acting_matches_emotion": None, "artefacts": [], "severity": "none", "category": None, **over})


# ---------------------------------------------------------------- routing and the action table

def test_fallback_methods_follow_the_guide():
    assert [routing.method_tier(t, True, 1) for t in ("A", "B1", "B2", "C")] == ["C", "B2", "C", "B2"]          # C with speech: Seedance + lip-sync
    assert routing.method_tier("C", True, 0) == "A" and routing.method_tier("B2", False, 1) == "B2" and routing.method_tier("INSERT", True, 0) == "INSERT"
    assert routing.model_key("A", True) == "kling_v3_pro" and routing.model_key("B1", True, 1) == "seedance_1_5_pro" and routing.model_key("C", True, 0) == "seedance_1_5_pro"
    assert not routing.needs_lipsync("B1", 2) and routing.needs_lipsync("B1", 2, True) and routing.needs_lipsync("B2", 2) and not routing.needs_lipsync("A", 0, True)


def test_the_action_table_row_by_row():
    assert plan({}, ("lipsync", "lipsync"))["action"] == "lipsync" and plan({}, ("lipsync", "lipsync"))["set"] == {"status": "rendered", "sync_attempts": 1}   # B2: only the lip-sync
    assert plan({"tier": "C"}, ("lipsync", "lipsync"))["action"] == "lipsync"
    b1 = plan({"tier": "B1"}, ("lipsync", "lipsync"))
    assert b1["action"] == "rerender" and b1["set"]["status"] == "keyframe_ready" and b1["set"]["attempts"] == 1
    assert plan({"sync_attempts": 2}, ("lipsync", "lipsync"))["action"] == "rerender"                          # two lip-sync redos used up
    assert plan({}, ("face_match", "face"))["set"] == {"status": "planned", "attempts": 1, "sync_attempts": 0, "keyframe_r2_key": None}
    assert plan({}, ("vision", "face"))["action"] == "keyframe" and plan({}, ("vision", "outfit"))["action"] == "keyframe"
    for cat in ("action", "acting", "artefact"):
        p = plan({}, ("vision", cat))
        assert p["action"] == "rerender" and p["set"]["status"] == "keyframe_ready"
    c = plan({}, ("clipping", "clipping"))
    assert c["action"] == "audio" and c["set"] == {"status": "ready"} and c["spec"] == {"audio_gain_db": -3.0} and not c.get("set", {}).get("attempts")
    assert plan({"spec": {"audio_gain_db": -3.0}}, ("clipping", "clipping"))["spec"] == {"audio_gain_db": -6.0}
    assert plan({"spec": {"audio_gain_db": -9.0}}, ("clipping", "clipping"))["action"] == "needs_human"          # still too loud at -9 dB
    assert repair.decide(shot_row(), []) is None


def test_attempts_two_switches_to_the_fallback_and_three_is_a_human_matter():
    p = plan({"attempts": 2, "tier": "B1", "planned_duration": 2.4}, ("vision", "action"))
    assert p["action"] == "fallback" and p["set"] == {"status": "keyframe_ready", "attempts": 3, "sync_attempts": 0, "fallback_used": True, "render_duration": 4}   # the B2 method: 4 s minimum
    assert plan({"attempts": 2, "fallback_used": True}, ("vision", "action"))["action"] == "rerender"
    for fails in (("vision", "action"), ("face_match", "face")):
        p = plan({"attempts": 3}, fails)
        assert p["action"] == "needs_human" and p["set"] == {"status": "needs_human"} and not p["reset"]
    assert plan({"attempts": 3}, ("lipsync", "lipsync"))["action"] == "lipsync"                                # a lip-sync redo costs no attempt


def test_the_earliest_stage_wins_when_several_checks_fail():
    p = plan({}, ("clipping", "clipping"), ("vision", "action"), ("face_match", "face"))
    assert p["action"] == "keyframe"
    assert plan({}, ("clipping", "clipping"), ("lipsync", "lipsync"))["action"] == "lipsync"


def test_unscored_lipsync_is_never_a_failure_to_repair(env):
    # failures() leaves out category 'unscored'
    sid = start(client())
    ep = db.fetch_one("insert into episodes (series_id, number, batch, status, beat) values (%s, 1, 'pilot', 'compiled', '{}') returning id", (sid,))["id"]
    shot = db.fetch_one("insert into shots (series_id, episode_id, idx, shot_type, tier, spec, status) values (%s, %s, 0, 'non_speaking', 'A', '{}', 'qa_failed') returning id", (sid, ep))["id"]
    db.execute("insert into qa_results (series_id, episode_id, shot_id, qa_round, check_name, passed, category) values (%s,%s,%s,'qa1','lipsync',false,'unscored')", (sid, ep, shot))
    db.execute("insert into qa_results (series_id, episode_id, shot_id, qa_round, check_name, passed, category) values (%s,%s,%s,'qa1','vision',false,'action')", (sid, ep, shot))
    assert repair.failures(shot) == [("vision", "action")]


# ---------------------------------------------------------------- the individual shot checks

def test_the_null_scorer_marks_lipsync_unscored_and_a_real_scorer_is_judged_by_thresholds():
    assert lipsync.get_scorer().name == "none" and lipsync.get_scorer().score("x.mp4") is None
    assert lipsync.judge(None, 3.0, 3) == (False, "unscored", None, {"scorer": "none"})
    ok = lipsync.judge({"confidence": 4.2, "offset_frames": -2}, 3.0, 3)
    assert ok[0] and ok[1] is None and ok[2] == 4.2
    assert lipsync.judge({"confidence": 2.0, "offset_frames": 0}, 3.0, 3)[1] == "lipsync"
    assert lipsync.judge({"confidence": 5.0, "offset_frames": 4}, 3.0, 3)[1] == "lipsync"


def test_the_vision_pass_rule():
    assert vision.passed(vq()) and vision.passed(vq(severity="minor", artefacts=["flicker"]))
    assert not vision.passed(vq(severity="major", category="artefact")) and not vision.passed(vq(face_consistent=False)) and not vision.passed(vq(outfit_correct=False)) and not vision.passed(vq(action_happens=False))


def test_face_match_uses_the_median_and_passes_when_there_is_no_face(monkeypatch, tmp_path):
    frames = []
    for i in range(4):
        f = tmp_path / f"f{i}.png"
        f.write_bytes(b"x" + bytes([i]))
        frames.append(f)
    master = face.stub_vector("master")
    other = face.stub_vector("somebody else")
    vectors = iter([master, master, other, None])
    monkeypatch.setattr(face, "face_vector", lambda data: next(vectors))
    ok, score, detail = shot_qa.face_match(frames, master, 0.36)               # similarities 1, 1, ~0 (one frame without a face): median 1
    assert ok and score == pytest.approx(1.0) and detail["frames_with_face"] == 3
    vectors = iter([other, other, master, other])
    monkeypatch.setattr(face, "face_vector", lambda data: next(vectors))
    assert shot_qa.face_match(frames, master, 0.36)[0] is False                # median of mostly-wrong faces
    monkeypatch.setattr(face, "face_vector", lambda data: None)
    assert shot_qa.face_match(frames, master, 0.36) == (True, None, {"no_face": True})


def test_clipping_uses_the_peak_level_and_the_gain_applied(tmp_path):
    loud, quiet = tmp_path / "loud.wav", tmp_path / "quiet.wav"
    for path, vol in ((loud, "18.5dB"), (quiet, "6dB")):
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-af", f"volume={vol}", str(path)], check=True)
    assert shot_qa.clipping(loud, 0, -0.5)[0] is False and shot_qa.clipping(quiet, 0, -0.5)[0] is True
    assert shot_qa.clipping(loud, -3.0, -0.5)[0] is True                       # the repair's -3 dB is counted
    silent = tmp_path / "s.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1", str(silent)], check=True)
    assert shot_qa.clipping(silent, 0, -0.5)[2] == {"no_audio": True}


# ---------------------------------------------------------------- episode checks

def test_runtime_hook_and_cliffhanger_checks():
    assert eq.runtime_check([10] * 8)[0] and not eq.runtime_check([10] * 7)[0] and not eq.runtime_check([10] * 10)[0]
    assert eq.hook_check({"is_hook": True}, 3.5, 4.0)[0] and not eq.hook_check({"is_hook": True}, 4.5, 4.0)[0] and not eq.hook_check({"is_hook": False}, 2, 4.0)[0]
    assert eq.cliffhanger_check({"is_cliffhanger": True})[0] and not eq.cliffhanger_check({})[0]


def test_loudness_is_measured_against_minus_16_lufs(tmp_path):
    def tone(name, filt):
        out = tmp_path / name
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anoisesrc=d=8:c=pink:r=48000", "-af", filt, str(out)], check=True)
        return out

    good = tone("good.wav", "loudnorm=I=-16:TP=-1.5:LRA=11")
    quiet = tone("quiet.wav", "loudnorm=I=-30:TP=-1.5:LRA=11")
    assert eq.loudness_check(good, -16, 1.5)[0] is True and eq.integrated_loudness(quiet) < -25
    assert eq.loudness_check(quiet, -16, 1.5)[0] is False


def test_the_captions_check_wants_every_word_and_no_overlap():
    ass = "[Events]\nDialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,one two three\nDialogue: 0,0:00:02.00,0:00:03.00,Default,,0,0,0,,four five\n"
    assert eq.captions_check(ass, ["one two three four five"])[0]
    assert not eq.captions_check(ass, ["one two three four five six"])[0]                               # a word has no caption
    overlapping = ass.replace("0:00:02.00,0:00:03.00", "0:00:01.50,0:00:03.00")
    assert eq.captions_check(overlapping, ["one two three four five"])[2]["overlaps"]


def test_continuity_contact_sheet_and_verdict(tmp_path):
    from PIL import Image

    frames = []
    for i in range(8):
        p = tmp_path / f"{i}.png"
        Image.new("RGB", (1080, 1920), (i * 30, 40, 90)).save(p)
        frames.append((i, p))
    sheet = Image.open(__import__("io").BytesIO(eq.contact_sheet(frames)))
    assert sheet.size == (eq.COLUMNS * eq.TILE[0], 2 * eq.TILE[1])
    ok, issues, _ = eq.continuity_check(None, fixture=lambda: eq.ContinuityQA(issues=[eq.ContinuityIssue(shot_idx=3, issue="hair colour changes", severity="minor")]))
    assert ok and issues[0]["shot_idx"] == 3
    assert eq.continuity_check(None, fixture=lambda: eq.ContinuityQA(issues=[eq.ContinuityIssue(shot_idx=3, issue="different person", severity="major")]))[0] is False


# ---------------------------------------------------------------- QA handlers on a real compiled pilot

def qa_job(sid, shot, rnd="qa1"):
    return {"id": uuid.uuid4(), "series_id": sid, "episode_id": shot["episode_id"], "shot_id": shot["id"], "kind": "qa_shot", "request": {"round": rnd}}


def one_shot(sid, tier="B2"):
    return db.fetch_one("select * from shots where series_id = %s and tier = %s order by idx limit 1", (sid, tier))


def test_qa_shot_writes_one_row_per_check_and_sets_the_status(pilot):
    shot = one_shot(pilot)
    out = Q.qa_shot(qa_job(pilot, shot, "qa2"))
    rows = {r["check_name"]: r for r in db.fetch_all("select * from qa_results where shot_id = %s and qa_round = 'qa2'", (shot["id"],))}
    assert set(rows) == {"face_match", "lipsync", "vision", "clipping"}
    assert rows["lipsync"]["passed"] is False and rows["lipsync"]["category"] == "unscored" and out["unscored"] == ["lipsync"]
    assert rows["vision"]["passed"] and rows["face_match"]["detail"]["dry_run"] is True
    assert db.fetch_one("select status from shots where id = %s", (shot["id"],))["status"] == "qa_passed"      # unscored is not a failure
    Q.qa_shot(qa_job(pilot, shot, "qa2"))                                                                      # running again replaces the rows
    assert db.fetch_one("select count(*) n from qa_results where shot_id = %s and qa_round = 'qa2'", (shot["id"],))["n"] == 4


def test_a_wrong_face_fails_face_match_and_resets_the_shot_to_planned(pilot, monkeypatch):
    shot = one_shot(pilot, "A")
    monkeypatch.setattr(look_handlers, "_real_faces", lambda: True)
    monkeypatch.setattr(face, "face_vector", lambda data: face.stub_vector("a different person"))
    out = Q.qa_shot(qa_job(pilot, shot))
    assert out["failed"] == ["face_match"]
    row = db.fetch_one("select * from qa_results where shot_id = %s and check_name = 'face_match'", (shot["id"],))
    assert row["passed"] is False and row["category"] == "face" and row["score"] is not None
    failed = db.fetch_one("select s.*, cardinality(s.line_ids) line_count from shots s where id = %s", (shot["id"],))
    p = repair.decide(failed, repair.failures(shot["id"]))
    assert p["action"] == "keyframe" and p["set"]["status"] == "planned" and p["set"]["keyframe_r2_key"] is None


def test_a_major_vision_problem_fails_the_shot(pilot, monkeypatch):
    shot = one_shot(pilot, "A")
    monkeypatch.setattr(vision, "fixture_pass", lambda: vq(severity="major", category="artefact", artefacts=["extra fingers"]))
    assert Q.qa_shot(qa_job(pilot, shot))["failed"] == ["vision"]
    row = db.fetch_one("select * from qa_results where shot_id = %s and check_name = 'vision'", (shot["id"],))
    assert row["category"] == "artefact" and row["detail"]["artefacts"] == ["extra fingers"]


def test_hero_shots_use_the_strong_vision_model(pilot, monkeypatch):
    seen = []
    real = vision.check
    monkeypatch.setattr(vision, "check", lambda role, *a, **k: (seen.append(role), real(role, *a, **k))[1])
    shot = one_shot(pilot, "A")
    db.execute("update shots set shot_type = 'hero' where id = %s", (shot["id"],))
    Q.qa_shot(qa_job(pilot, shot))
    Q.qa_shot(qa_job(pilot, one_shot(pilot, "B1")))
    assert seen == ["vision_qa_strong", "vision_qa"]


def test_the_episode_qa_rows_on_a_real_compile(pilot):
    from app.jobs.handlers import build_handlers as B

    ep = db.fetch_one("select * from episodes where series_id = %s and number = 1", (pilot,))
    B.ffmpeg_compile(job_for(pilot, "ffmpeg_compile", ep["id"], episode=1, cut=9))
    out = Q.qa_episode({"id": uuid.uuid4(), "series_id": pilot, "episode_id": ep["id"], "kind": "qa_episode", "request": {"round": "qa1"}})
    rows = {r["check_name"]: r for r in db.fetch_all("select * from qa_results where episode_id = %s and shot_id is null and qa_round = 'qa1'", (ep["id"],))}
    assert set(rows) == {"runtime", "hook", "cliffhanger", "loudness", "continuity", "captions", "lipsync_review"}
    assert rows["captions"]["passed"], rows["captions"]["detail"]                                             # the real caption file covers every line with no overlap
    assert rows["runtime"]["passed"] and rows["cliffhanger"]["passed"] and rows["loudness"]["score"] is not None
    assert "judged by people" in rows["lipsync_review"]["detail"]["note"] and rows["lipsync_review"]["detail"]["unscored_shots"]
    assert "captions" not in out["failed"]


# ---------------------------------------------------------------- learned rules

def test_a_near_duplicate_rule_increments_hits_instead_of_inserting(env):
    a, inserted, _ = learning.store_rule("seedance_1_5_pro", "Keep hands out of frame when the character speaks.", "artefact", None)
    assert inserted
    b, inserted, _ = learning.store_rule("seedance_1_5_pro", "Keep hands out of frame when the character speaks.", "artefact", None)
    assert not inserted and a == b
    assert db.fetch_one("select hits, (select count(*) from learned_rules) n from learned_rules")["hits"] == 1
    c, inserted, _ = learning.store_rule("seedance_1_5_pro", "Never describe jewellery in a close-up prompt.", "artefact", None)
    assert inserted and c != a
    d, inserted, _ = learning.store_rule("kling_v3_pro", "Keep hands out of frame when the character speaks.", "artefact", None)      # another model has its own rules
    assert inserted and d != a
    assert db.fetch_one("select count(*) n from learned_rules")["n"] == 3


def test_the_rule_job_writes_one_short_rule_for_the_failed_shot(pilot, monkeypatch):
    shot = one_shot(pilot, "A")
    db.execute("update shots set status = 'qa_failed' where id = %s", (shot["id"],))
    db.execute("insert into qa_results (series_id, episode_id, shot_id, qa_round, check_name, passed, category, detail) values (%s,%s,%s,'qa1','vision',false,'artefact','{\"artefacts\": [\"extra fingers\"]}')", (pilot, shot["episode_id"], shot["id"]))
    seen = []
    real = learning.llm.complete
    monkeypatch.setattr(learning.llm, "complete", lambda role, system, messages, **k: (seen.append((role, messages[0]["content"])), real(role, system, messages, **k))[1])
    out = Q.llm_rule({"id": uuid.uuid4(), "series_id": pilot, "shot_id": shot["id"], "kind": "llm_rule", "request": {"check": "vision", "round": 0}})
    role, text = seen[0]
    assert role == "rule_writer" and "seedance_1_5_pro" in text and "extra fingers" in text and "Prompt that was sent" in text
    rule = db.fetch_one("select * from learned_rules where id = %s", (out["rule_id"],))
    assert out["inserted"] and rule["model_key"] == "seedance_1_5_pro" and rule["category"] == "artefact" and rule["source_shot_id"] == shot["id"] and rule["active"]
    again = Q.llm_rule({"id": uuid.uuid4(), "series_id": pilot, "shot_id": shot["id"], "kind": "llm_rule", "request": {"check": "vision", "round": 1}})
    assert not again["inserted"] and again["rule_id"] == out["rule_id"]
    monkeypatch.setattr(learning, "write_rule", lambda s, c, fixture=None: (learning.RuleOut(rule=" ".join(["word"] * 30)), {"cost_usd": 0.0}))
    from app.jobs.errors import PermanentError

    with pytest.raises(PermanentError, match="at most 25 words"):
        Q.llm_rule({"id": uuid.uuid4(), "series_id": pilot, "shot_id": shot["id"], "kind": "llm_rule", "request": {"check": "vision", "round": 2}})


def test_weekly_retirement(env):
    for hits, wins in ((20, 5), (20, 10), (19, 0), (40, 30)):
        db.execute("insert into learned_rules (model_key, rule_text, category, hits, wins) values ('seedance_1_5_pro', %s, 'x', %s, %s)", (f"rule {hits}/{wins}", hits, wins))
    assert learning.retire_rules() == 1
    assert {r["rule_text"] for r in db.fetch_all("select rule_text from learned_rules where not active")} == {"rule 20/5"}
    import importlib

    assert hasattr(importlib.import_module("scripts.retire_rules"), "main")


# ---------------------------------------------------------------- the whole loop in the graph

def to_pilot_gate(runner, failing=None, scorer=None, monkeypatch=None):
    """Drive a production to Gate 4. `failing(shot)` may return a VisionQA to use for that shot's vision check;
    `scorer(shot)` may return a lipsync result dict (or None)."""
    from helpers import at_gate, drive

    c = client()
    sid = start(c)
    orig = HANDLERS["qa_shot"].run

    def run(job):
        shot = db.fetch_one("select s.*, cardinality(s.line_ids) line_count from shots s where s.id = %s", (job["shot_id"],))
        if failing:
            result = failing(shot)
            monkeypatch.setattr(vision, "fixture_pass", (lambda r=result: r) if result else (lambda: vq()))
        if scorer:
            result = scorer(shot)
            monkeypatch.setattr(lipsync, "_scorer", SimpleNamespace(name="fake", score=lambda path, r=result: r))
        return orig(job)

    monkeypatch.setattr(HANDLERS["qa_shot"], "run", run)
    to_gate(runner, c, sid, 4)
    return sid


def jobs_for(sid, kind, shot_id):
    return db.fetch_all("select * from jobs where series_id = %s and kind = %s and shot_id = %s order by created_at", (sid, kind, shot_id))


def episode1_shot(sid, idx):
    return db.fetch_one("select s.*, e.number from shots s join episodes e on e.id = s.episode_id where s.series_id = %s and e.number = 1 and s.idx = %s", (sid, idx))


def test_a_failed_vision_check_is_repaired_and_the_clip_rendered_again(runner, monkeypatch):
    seen = {}

    def failing(shot):
        key = str(shot["id"])
        if shot["idx"] == 0 and shot["attempts"] == 0 and "done" not in seen and db.fetch_one("select e.number from episodes e where e.id = %s", (shot["episode_id"],))["number"] == 1:
            seen["done"] = key
            return vq(severity="major", category="action", action_happens=False)
        return None

    sid = to_pilot_gate(runner, failing=failing, monkeypatch=monkeypatch)
    shot = db.fetch_one("select * from shots where id = %s", (seen["done"],))
    assert shot["attempts"] == 1 and shot["status"] == "qa_passed" and not shot["fallback_used"]
    assert len(jobs_for(sid, "video_clip", shot["id"])) == 2 and len(jobs_for(sid, "llm_shot_request", shot["id"])) == 2      # a new prompt and a new render
    assert len(jobs_for(sid, "image_keyframe", shot["id"])) <= 1                                                           # the keyframe is kept for a clip re-render
    assert db.fetch_one("select count(*) n from audit_log where series_id = %s and action like 'note:repair:1:0'", (sid,))["n"] == 1
    assert db.fetch_one("select cut_version from episodes where series_id = %s and number = 1", (sid,))["cut_version"] >= 1
    assert db.fetch_one("select status from episodes where series_id = %s and number = 1", (sid,))["status"] == "qa_passed"
    assert db.fetch_one("select count(*) n from jobs where series_id = %s and kind = 'llm_rule'", (sid,))["n"] >= 1             # the failure became a rule
    assert db.fetch_one("select count(*) n from learned_rules")["n"] == 1


def test_a_lipsync_failure_on_b2_redoes_only_the_lipsync(runner, monkeypatch):
    seen = {}

    def scorer(shot):
        if shot["tier"] == "B2" and shot["sync_attempts"] == 0 and "id" not in seen:
            seen["id"] = shot["id"]
            return {"confidence": 1.0, "offset_frames": 9}
        return {"confidence": 5.0, "offset_frames": 0}

    sid = to_pilot_gate(runner, scorer=scorer, monkeypatch=monkeypatch)
    shot = db.fetch_one("select * from shots where id = %s", (seen["id"],))
    assert shot["sync_attempts"] == 1 and shot["attempts"] == 0 and shot["status"] == "qa_passed"
    assert len(jobs_for(sid, "lipsync", shot["id"])) == 2 and len(jobs_for(sid, "video_clip", shot["id"])) == 1 and len(jobs_for(sid, "image_keyframe", shot["id"])) <= 1
    assert db.fetch_one("select count(*) n from learned_rules")["n"] == 1 and db.fetch_one("select category from learned_rules")["category"] == "lipsync"
    assert db.fetch_one("select lipsync_score_n n from camera_setups limit 1")["n"] > 0                                        # scored shots feed the camera library


def test_the_loop_ends_after_round_three_and_a_stubborn_shot_uses_the_fallback_route(runner, monkeypatch):
    def failing(shot):
        n = db.fetch_one("select e.number from episodes e where e.id = %s", (shot["episode_id"],))["number"]
        return vq(severity="major", category="action", action_happens=False) if (n == 1 and shot["idx"] == 0) else None

    sid = to_pilot_gate(runner, failing=failing, monkeypatch=monkeypatch)
    shot = episode1_shot(sid, 0)
    assert shot["status"] == "qa_failed" and shot["attempts"] == 3 and shot["fallback_used"]                                # re-rendered twice, then the fallback route, then stopped
    clips = jobs_for(sid, "video_clip", shot["id"])
    assert len(clips) == 4 and clips[0]["endpoint"] == falmedia.SEEDANCE and clips[-1]["endpoint"] == falmedia.KLING_V3        # tier A falls back to Kling V3 Pro
    assert clips[-1]["request"]["arguments"]["generate_audio"] is True
    s = db.fetch_one("select status, current_stage from series where id = %s", (sid,))
    assert s["status"] == "waiting_gate" and s["current_stage"] == "gate4"
    assert db.fetch_one("select status from episodes where series_id = %s and number = 1", (sid,))["status"] == "qa_passed"   # flagged shots are listed at the gate, the episode is not stuck


def test_nothing_failing_means_no_repair_round_and_the_camera_library_learns(runner, monkeypatch):
    sid = to_pilot_gate(runner, monkeypatch=monkeypatch)
    assert db.fetch_one("select count(*) n from audit_log where series_id = %s and action like 'note:repair:%%'", (sid,))["n"] == 0
    cam = db.fetch_one("select * from camera_setups where name = 'Static medium'")
    shots = db.fetch_all("select * from shots where series_id = %s", (sid,))
    assert cam["uses"] == len(shots) and cam["passes"] == len(shots)
    assert set(cam["prompt_by_model"]) == {"seedance_1_5_pro", "kling_avatar_v2_pro"}                                       # the first wording that passed, per model
    assert all(s["spec"].get("learn_counted") for s in shots)
    before = db.fetch_one("select uses from camera_setups where id = %s", (cam["id"],))["uses"]
    learning.apply_learning(sid, "pilot")                                                                                  # running the learn step again counts nothing twice
    assert db.fetch_one("select uses from camera_setups where id = %s", (cam["id"],))["uses"] == before


def test_rule_wins_and_hits_follow_the_shots_that_used_them(runner, monkeypatch):
    sid = to_pilot_gate(runner, monkeypatch=monkeypatch)
    (vec,), _ = images.embed(["rule"])
    rid = db.fetch_one("insert into learned_rules (model_key, rule_text, category, hits, embedding) values ('seedance_1_5_pro', 'A rule.', 'x', 1, %s::vector) returning id", (db.to_vector(vec),))["id"]
    shot = db.fetch_one("select s.* from shots s where s.series_id = %s and s.tier = 'A' order by idx limit 1", (sid,))
    db.execute("update shots set spec = spec - 'learn_counted' where id = %s", (shot["id"],))
    db.execute("update jobs set result = result || %s::jsonb where kind = 'llm_shot_request' and shot_id = %s", (json.dumps({"rule_ids": [str(rid)]}), shot["id"]))
    learning.apply_learning(sid, "pilot")
    assert db.fetch_one("select wins from learned_rules where id = %s", (rid,))["wins"] == 1
    db.execute("update shots set attempts = 1 where id = %s", (shot["id"],))                                               # a shot that needed a repair earns no win
    db.execute("update shots set spec = spec - 'learn_counted' where id = %s", (shot["id"],))
    learning.apply_learning(sid, "pilot")
    assert db.fetch_one("select wins from learned_rules where id = %s", (rid,))["wins"] == 1


def test_a_setup_that_keeps_failing_on_a_model_learns_to_avoid_it(runner, monkeypatch):
    sid = to_pilot_gate(runner, monkeypatch=monkeypatch)
    cam = db.fetch_one("select id from camera_setups where name = 'Static medium'")["id"]
    assert db.fetch_one("select avoid_models from camera_setups where id = %s", (cam,))["avoid_models"] == []
    db.execute("update shots set attempts = 1 where camera_setup_id = %s and tier in ('A','B2')", (cam,))                      # none of the Seedance shots passed first time
    assert learning.update_avoid_models() == 1
    assert db.fetch_one("select avoid_models from camera_setups where id = %s", (cam,))["avoid_models"] == ["seedance_1_5_pro"]
    assert learning.update_avoid_models() == 0                                                                             # and only once
    from app.stages import shots as S

    rows, _ = S.retrieve_setups(sid, "anything")
    assert rows == []                                                                                                      # the director is no longer offered it
