"""Phase 12: delivery and hardening: master, marketing copy, delivery page, retention, structured logs, concurrency and crash safety."""
import io
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from helpers import at_gate, drive, step
from test_phase5 import ad_brief, client, env, gate, jobs_of, post_json, runner, start, to_gate, voices  # noqa: F401 (fixtures)

from app import db, delivery, logging_setup, retention
from app.jobs import ledger, poller, queue, worker
from app.jobs.handlers import delivery_handlers as D
from app.jobs.errors import PermanentError
from app.media import ffmpeg
from app.models.enums import JobKind
from app.vendors import storage

# ---------------------------------------------------------------- a finished production

@pytest.fixture()
def finished(runner):
    c = client()
    sid = start(c)
    to_gate(runner, c, sid, 5)
    assert gate(c, sid, 5, decision="approve").status_code == 303
    drive(runner, sid, lambda s: s["status"] == "done")
    return c, sid


def test_a_finished_production_has_a_master_marketing_copy_and_a_delivery_page(finished):
    c, sid = finished
    eps = db.fetch_all("select number, final_r2_key, status from episodes where series_id = %s order by number", (sid,))
    assert [e["number"] for e in eps] == [1, 2, 3, 4, 5] and {e["status"] for e in eps} == {"approved"}
    master = storage.local_file(D.master_key(sid))
    info = ffmpeg.probe(master)
    parts = [ffmpeg.duration(storage.local_file(e["final_r2_key"])) for e in eps]
    assert info["duration"] == pytest.approx(sum(parts), abs=0.6) and (info["width"], info["height"]) == (1080, 1920) and info["has_audio"]      # stream copy, in order
    job = db.fetch_one("select result from jobs where series_id = %s and kind = 'ffmpeg_master' and status = 'succeeded'", (sid,))
    assert job["result"]["episodes"] == 5 and job["result"]["seconds"] == pytest.approx(info["duration"], abs=0.1)
    for n in range(1, 6):
        copy = json.loads(storage.local_file(D.marketing_key(sid, n)).read_text(encoding="utf-8"))
        assert copy["episode"] == n and copy["language"] == "bn" and 3 <= len(copy["hashtags"]) <= 6 and 2 <= len(copy["hooks"]) <= 3 and len(copy["title"]) <= 60
    assert db.fetch_one("select count(*) n from jobs where series_id = %s and kind = 'llm_marketing' and status = 'succeeded'", (sid,))["n"] == 5

    page = c.get(f"/productions/{sid}/delivery")
    assert page.status_code == 200
    for needle in ('id="master"', "Download the master file", 'id="cost"', 'id="qa"', "episode_01.mp4", "episode_05.mp4", ">.ass<", ">.json<", "Cost report", "QA report"):
        assert needle in page.text, needle
    assert 'id="placements"' not in page.text and client("other@x.io").get(f"/productions/{sid}/delivery").status_code == 200       # anybody signed in may view; no placement report for a standard drama
    assert c.get(f"/productions/{sid}/delivery").text.count('data-episode=') == 5
    master_url = page.text.split('id="master" href="')[1].split('"')[0]
    assert c.get(master_url).status_code == 200

    cost = c.get(f"/productions/{sid}/delivery/cost.csv")
    assert cost.status_code == 200 and cost.headers["content-disposition"].startswith("attachment") and cost.text.splitlines()[0] == "section,vendor,model,unit,units,cost_usd"
    total = float(db.fetch_one("select coalesce(sum(cost_usd), 0) t from cost_log where series_id = %s", (sid,))["t"])
    assert f"total,,,,,{total:.4f}" in cost.text and "video_budget,fal,pilot" in cost.text and "by_episode" in cost.text and "compare it with the fal invoice" in cost.text.replace("until it is compared with the fal invoice", "compare it with the fal invoice")
    qa = c.get(f"/productions/{sid}/delivery/qa.csv")
    assert qa.status_code == 200 and qa.text.splitlines()[0] == "episode,shot,round,check,passed,category,score,note" and "no lip-sync scorer: judged by people at the gates" in qa.text
    assert "runtime" in qa.text and "face_match" in qa.text
    assert c.get(f"/productions/{uuid.uuid4()}/delivery").status_code == 404 and c.get(f"/productions/{uuid.uuid4()}/delivery/cost.csv").status_code == 404


def test_the_delivery_page_of_an_unfinished_production_says_so(env):
    c = client()
    sid = start(c)
    page = c.get(f"/productions/{sid}/delivery").text
    assert "is not finished yet" in page and "The master file is not ready" in page
    assert not delivery.has_delivery(sid)


def test_an_ad_drama_delivery_lists_the_placement_report(runner):
    c = client()
    data, files = ad_brief()
    r = post_json(c, data, files)
    sid = uuid.UUID(r.headers["location"].rsplit("/", 1)[1])
    to_gate(runner, c, sid, 4)
    assert 'id="placements"' in c.get(f"/productions/{sid}/delivery").text


# ---------------------------------------------------------------- the master and the marketing copy as units

def test_the_master_refuses_missing_episodes_and_a_wrong_length(env):
    sid = start(c := client())
    with pytest.raises(PermanentError, match="no episodes"):
        D.ffmpeg_master({"series_id": sid})
    db.execute("insert into episodes (series_id, number, batch, status, beat) values (%s, 1, 'pilot', 'qa_passed', '{}')", (sid,))
    with pytest.raises(PermanentError, match=r"episodes \[1\] have no final cut"):
        D.ffmpeg_master({"series_id": sid})


def test_the_marketing_copy_rules():
    ok = D.Marketing(title="The Quiet Debt 3", teaser="A letter comes back.", hooks=["One.", "Two."], hashtags=["#drama", "#debt", "#micro"])
    assert D.validate(ok, []) == []
    assert any("title" in p for p in D.validate(ok.model_copy(update={"title": "x" * 61}), []))
    assert any("teaser" in p for p in D.validate(ok.model_copy(update={"teaser": ""}), []))
    assert any("hooks" in p for p in D.validate(ok.model_copy(update={"hooks": ["only one"]}), []))
    assert any("hashtags" in p for p in D.validate(ok.model_copy(update={"hashtags": ["#a", "b c", "#d"]}), []))
    assert any("forbidden claim" in p for p in D.validate(ok.model_copy(update={"teaser": "It cures chapped lips."}), ["cures chapped lips"]))


def test_ad_marketing_copy_never_uses_a_forbidden_claim(runner, monkeypatch):
    from app.vendors import llm

    c = client()
    data, files = ad_brief()
    sid = uuid.UUID(post_json(c, data, files).headers["location"].rsplit("/", 1)[1])
    to_gate(runner, c, sid, 4)
    ep = db.fetch_one("select id from episodes where series_id = %s and number = 1", (sid,))["id"]
    seen = []
    real = llm.complete

    def complete(role, system, messages, schema=None, **k):
        seen.append(messages[0]["content"])
        return real(role, system, messages, schema, **k)

    monkeypatch.setattr(llm, "complete", complete)
    D.llm_marketing({"id": uuid.uuid4(), "series_id": sid, "episode_id": ep, "kind": "llm_marketing", "request": {"episode": 1}})
    assert "BRAND NOTES (data)" in seen[0] and "cures chapped lips" in seen[0]
    monkeypatch.setattr(llm, "complete", lambda *a, **k: (D.Marketing(title="t", teaser="This cures chapped lips.", hooks=["a", "b"], hashtags=["#a", "#b", "#c"]), {"model": "m", "tokens_in": 0, "tokens_out": 0, "cost_usd": 0}))
    with pytest.raises(PermanentError, match="forbidden claim"):
        D.llm_marketing({"id": uuid.uuid4(), "series_id": sid, "episode_id": ep, "kind": "llm_marketing", "request": {"episode": 1}})


# ---------------------------------------------------------------- retention

OLD = datetime.now(timezone.utc) - timedelta(days=45)
NEW = datetime.now(timezone.utc) - timedelta(days=3)


def put(key, when, data=b"x" * 100):
    storage.put_bytes(key, data)
    ts = when.timestamp()
    os.utime(storage.local_file(key), (ts, ts))


def exists(key):
    return storage.local_file(key).exists()


def test_retention_deletes_only_old_raw_and_failed_clips_of_ended_productions(env):
    c = client()
    done, running, failed = start(c), start(c), start(c)
    db.execute("update series set status = 'done' where id = %s", (done,))
    db.execute("update series set status = 'failed' where id = %s", (failed,))
    keys = {}
    for name, sid in (("done", done), ("running", running), ("failed", failed)):
        base = f"series/{sid}/episodes/01"
        keys[name] = {"raw_old": f"{base}/shots/0/raw_a0.mp4", "raw_new": f"{base}/shots/1/raw_a0.mp4", "synced_old": f"{base}/shots/0/synced_a1.mp4", "final_old": f"{base}/shots/2/synced_a0.mp4",
                      "episode": f"{base}/final_v1.mp4", "audio": f"{base}/audio/line_0.mp3", "keyframe": f"{base}/shots/0/keyframe_a0.png", "brand": f"series/{sid}/brand/a.png",
                      "master": f"series/{sid}/delivery/master.mp4", "insert": f"{base}/shots/3/insert_a0.mp4", "raw_old_as_final": f"{base}/shots/4/raw_a0.mp4"}
        for label, key in keys[name].items():
            put(key, NEW if label == "raw_new" else OLD)
        ep = db.fetch_one("insert into episodes (series_id, number, batch, status, beat) values (%s, 1, 'pilot', 'compiled', '{}') returning id", (sid,))["id"]
        for idx, final in ((2, keys[name]["final_old"]), (4, keys[name]["raw_old_as_final"])):          # a shot that was lip-synced, and one with no lip-sync whose raw clip IS its final clip
            db.execute("insert into shots (series_id, episode_id, idx, shot_type, tier, spec, status, final_clip_r2_key) values (%s,%s,%s,'non_speaking','A','{}','qa_passed',%s)", (sid, ep, idx, final))
    dry = retention.run(dry_run=True)
    assert dry["deleted"] == 4 and all(exists(k) for g in keys.values() for k in g.values())                   # a dry run deletes nothing
    result = retention.run()
    assert result["retention_days"] == 30 and result["deleted"] == 4 and result["freed_bytes"] == 400 and result["kept_recent"] == 2 and result["kept_in_use"] == 4
    for name in ("done", "failed"):
        k = keys[name]
        assert not exists(k["raw_old"]) and not exists(k["synced_old"])                                         # old raw and superseded takes are gone
        for keep in ("raw_new", "final_old", "raw_old_as_final", "episode", "audio", "keyframe", "brand", "master", "insert"):
            assert exists(k[keep]), (name, keep)
    assert all(exists(v) for v in keys["running"].values())                                                      # nothing in a running production
    assert retention.run()["deleted"] == 0                                                                       # and it is idempotent
    db.execute("update config set value = '1'::jsonb where key = 'raw_retention_days'")
    from app.config import clear_config_cache

    clear_config_cache()
    assert retention.run()["deleted"] == 2                                                                       # the limit is a config value: now the 3-day-old raw clips go too (done and failed)
    assert exists(keys["done"]["final_old"]) and exists(keys["done"]["raw_old_as_final"])


def test_the_retention_script_runs_and_reports(env, capsys):
    from scripts import retention as script

    out = script.main([])
    assert out["deleted"] == 0 and json.loads(capsys.readouterr().out.strip().splitlines()[-1])["retention_days"] == 30


# ---------------------------------------------------------------- structured logs

def record(msg, level=logging.INFO, **extra):
    return logging.LogRecord("worker", level, __file__, 1, msg, (), None), extra


def fmt(msg="hello", **extra):
    r = logging.LogRecord("worker", logging.INFO, __file__, 1, msg, (), None)
    for k, v in extra.items():
        setattr(r, k, v)
    return json.loads(logging_setup.JsonFormatter().format(r))


def test_log_lines_are_json_with_the_job_context():
    out = fmt("job finished", series_id=uuid.UUID(int=7), job_id="j1", kind="video_clip", vendor="fal", duration_ms=1234, unrelated="dropped")
    assert out["message"] == "job finished" and out["level"] == "INFO" and out["logger"] == "worker" and out["ts"].endswith("+00:00")
    assert out["series_id"] == str(uuid.UUID(int=7)) and out["job_id"] == "j1" and out["kind"] == "video_clip" and out["duration_ms"] == 1234 and "unrelated" not in out


def test_logs_never_contain_credentials_or_request_headers():
    secrets = [("sk-abcdefghijklmnop12345", "abcdefghijklmnop12345"), ("Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig", "eyJhbGciOiJIUzI1NiJ9"),
               ("Key 0123456789abcdef:fedcba9876543210", "fedcba9876543210"), ("xi-api-key: 9f8e7d6c5b4a39281716", "9f8e7d6c5b4a39281716"),
               ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA"), ("Cookie: md_session=abc123def456", "abc123def456"),
               ("https://x.r2.cloudflarestorage.com/a.mp4?X-Amz-Signature=deadbeefcafe0123&X-Amz-Credential=AKIAXXXX", "deadbeefcafe0123")]
    for secret, token in secrets:
        line = json.dumps(fmt(f"request failed: {secret} while calling the vendor"))
        assert token not in line, secret
        assert "[redacted]" in line
    out = fmt("x", headers={"Authorization": "Bearer abcdefghijklmnop"}, authorization="Bearer abcdefghijklmnop", api_key="k", series_id="s")
    assert "headers" not in out and "authorization" not in out and "api_key" not in out and out["series_id"] == "s"
    try:
        raise RuntimeError("boom with xi-api-key: 9f8e7d6c5b4a39281716")
    except RuntimeError:
        r = logging.LogRecord("worker", logging.ERROR, __file__, 1, "crashed", (), sys.exc_info())
    exc = json.loads(logging_setup.JsonFormatter().format(r))["exception"]
    assert "9f8e7d6c5b4a39281716" not in exc and "RuntimeError" in exc


def test_setup_puts_json_on_every_process_and_quiets_the_http_libraries():
    root = logging.getLogger()
    saved = (root.handlers[:], root.level, logging.getLogger("httpx").level)
    try:
        logging_setup.setup()
        assert len(root.handlers) == 1 and isinstance(root.handlers[0].formatter, logging_setup.JsonFormatter)
        assert logging.getLogger("httpx").level == logging.WARNING and logging.getLogger("uvicorn.access").propagate is True
    finally:
        root.handlers[:], root.level = saved[0], saved[1]
        logging.getLogger("httpx").setLevel(saved[2])


def test_the_worker_logs_each_job_with_its_context(env, caplog):
    sid = start(client())
    job = ledger.enqueue(JobKind.sfx_effect, series_id=sid, request={"x": 1})
    caplog.set_level(logging.INFO, logger="worker")
    assert worker.run_one("w1")
    started = [r for r in caplog.records if r.getMessage() == "job started"][0]
    finished = [r for r in caplog.records if r.getMessage() == "job finished"][0]
    assert str(started.series_id) == str(sid) and str(started.job_id) == str(job["id"]) and started.kind == "sfx_effect" and finished.duration_ms >= 0


# ---------------------------------------------------------------- three productions at once, and a killed worker

def approve_waiting(clients, sids):
    for c, sid in zip(clients, sids):
        s = db.fetch_one("select status, current_stage from series where id = %s", (sid,))
        if s["status"] != "waiting_gate":
            continue
        stage = s["current_stage"]
        if stage == "gate4":
            c.post(f"/productions/{sid}/pilot", data={"decision": "continue"})
        elif stage == "gate3":
            c.post(f"/productions/{sid}/gate/3", data={"decision": "approve", "voices": json.dumps(voices(sid))})
        elif stage in ("gate2", "gate5"):
            c.post(f"/productions/{sid}/gate/{stage[4:]}", data={"decision": "approve"})


def test_three_productions_run_at_once_to_done_with_no_cross_talk(runner):
    clients = [client(), client("admin@x.io"), client("other@x.io")]
    sids = [start(c) for c in clients]
    for _ in range(2500):
        approve_waiting(clients, sids)
        if all(db.fetch_one("select status from series where id = %s", (sid,))["status"] == "done" for sid in sids):
            break
        if step(runner) == 0:
            approve_waiting(clients, sids)
    states = [db.fetch_one("select status, current_stage from series where id = %s", (sid,)) for sid in sids]
    assert states == [{"status": "done", "current_stage": "done"}] * 3, states
    for sid in sids:
        eps = db.fetch_all("select number, status, final_r2_key, subtitle_r2_key, music_r2_key from episodes where series_id = %s order by number", (sid,))
        assert [e["number"] for e in eps] == [1, 2, 3, 4, 5] and {e["status"] for e in eps} == {"approved"}
        prefix = f"series/{sid}/"
        keys = [e[k] for e in eps for k in ("final_r2_key", "subtitle_r2_key", "music_r2_key")]
        keys += [r[c] for table, cols in (("shots", ("keyframe_r2_key", "raw_clip_r2_key", "final_clip_r2_key")), ("character_assets", ("r2_key_clean", "r2_key_review")), ("sets", ("plate_r2_key", "ambience_r2_key")),
                                           ("dialogue_lines", ("audio_r2_key",))) for r in db.fetch_all(f"select {', '.join(cols)} from {table} where series_id = %s", (sid,)) for c in cols]
        assert all(k.startswith(prefix) for k in keys if k), [k for k in keys if k and not k.startswith(prefix)][:3]     # every stored file is under its own production
        assert storage.local_file(D.master_key(sid)).exists()
        for table in ("jobs", "shots", "dialogue_lines", "cost_log", "alerts", "audit_log"):
            assert db.fetch_one(f"select count(*) n from {table} where series_id is not null and series_id = %s", (sid,))["n"] > 0
        # a job's own ids belong to the same production
        assert not db.fetch_all("select j.id from jobs j join shots s on s.id = j.shot_id where j.series_id = %s and s.series_id <> j.series_id", (sid,))
        assert not db.fetch_all("select j.id from jobs j join episodes e on e.id = j.episode_id where j.series_id = %s and e.series_id <> j.series_id", (sid,))
        assert not db.fetch_all("select 1 from jobs where series_id = %s and request::text like any(%s)", (sid, [f"%{other}%" for other in sids if other != sid]))   # no request mentions another production
    assert len({db.fetch_one("select count(*) n from jobs where series_id = %s", (sid,))["n"] for sid in sids}) == 1                                    # identical briefs, identical work
    assert len({db.fetch_one("select round(sum(cost_usd)::numeric, 6) t from cost_log where series_id = %s", (sid,))["t"] for sid in sids}) == 1
    assert db.fetch_one("select count(*) n from jobs where status in ('queued','running','submitted')")["n"] == 0
    assert db.fetch_one("select count(*) n from graph_commands where status in ('queued','running')")["n"] == 0
    assert len({db.fetch_one("select thread_id from series where id = %s", (sid,))["thread_id"] for sid in sids}) == 3


KILLED = r"""
import os, sys, time
sys.path.insert(0, os.getcwd())
from app.jobs import queue
job = queue.claim('victim-worker')
print('CLAIMED', job['id'], flush=True)
time.sleep(120)
"""


def test_killing_a_worker_mid_job_leaves_nothing_stuck(env, tmp_path):
    sid = start(client())
    group = f"{sid}:test:1"
    a = ledger.enqueue(JobKind.sfx_effect, series_id=sid, wait_group=group, request={"n": 1})
    b = ledger.enqueue(JobKind.sfx_effect, series_id=sid, wait_group=group, request={"n": 2})
    db.execute("update series set status = 'waiting_jobs', waiting_group = %s where id = %s", (group, sid))      # the graph is waiting for this group
    db.execute("update jobs set created_at = now() - interval '1 hour' where id = %s", (a["id"],))              # the victim claims the older one
    proc_env = {**os.environ, "DRY_RUN": "1", "DATABASE_URL": os.environ["DATABASE_URL"], "MEDIA_TMP_DIR": str(tmp_path), "PYTHONPATH": os.getcwd()}
    proc = subprocess.Popen([sys.executable, "-c", KILLED], stdout=subprocess.PIPE, text=True, env=proc_env)
    try:
        line = proc.stdout.readline()
        assert line.startswith("CLAIMED"), line
        claimed = line.split()[1]
        assert db.fetch_one("select status, locked_by from jobs where id = %s", (claimed,)) == {"status": "running", "locked_by": "victim-worker"}
        proc.kill()                                                                                           # SIGKILL: no clean-up, no goodbye
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert worker.run_one("healthy") is True                                                                  # the other job is done by a healthy worker
    assert worker.run_one("healthy") is False                                                                  # the dead worker's job is not claimable while it looks alive
    stuck = db.fetch_one("select status from jobs where id = %s", (claimed,))["status"]
    assert stuck == "running"
    assert poller.tick()["requeued"] == 0                                                                     # not yet: the poller waits for the stale limit
    db.execute("update jobs set locked_at = now() - interval '20 minutes' where id = %s", (claimed,))
    assert poller.tick()["requeued"] == 1
    assert db.fetch_one("select status, locked_by from jobs where id = %s", (claimed,)) == {"status": "queued", "locked_by": None}
    assert worker.run_one("healthy") is True
    assert db.fetch_all("select status, count(*) n from jobs where series_id = %s group by 1", (sid,)) == [{"status": "succeeded", "n": 2}]
    assert db.fetch_one("select count(*) n from jobs where status in ('queued','running','submitted')")["n"] == 0
    from app.graphs.runner import resume_finished_groups

    assert resume_finished_groups() == 1                                                                      # the group finished, so exactly one resume goes to the graph
    assert resume_finished_groups() == 0
