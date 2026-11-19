"""The LatentSync lip-sync fallback on fal (docs/LIPSYNC_OPTIONS.md): Kling LipSync first, LatentSync for the redo and after Kling refuses a clip."""
import pytest
from test_phase5 import client, env, runner, start  # noqa: F401 (fixtures)

from app import db
from app.graphs import work
from app.jobs.errors import PermanentError
from app.media import costs, requests as media_requests, routing
from app.vendors import falmedia


def test_engine_choice():
    assert routing.lipsync_engine(0, 0) == "kling"
    assert routing.lipsync_engine(1, 0) == "latentsync"                      # the redo after a failed lip-sync check
    assert routing.lipsync_engine(0, 1) == "latentsync"                      # Kling failed or refused the clip
    assert routing.lipsync_engine(1, 1, "none") == "kling"                   # the fallback can be switched off in config


def test_requests_follow_the_fal_schemas():
    k = media_requests.lipsync_request("series/x/raw.mp4", "series/x/a.mp3")
    assert k == {"endpoint": falmedia.KLING_LIPSYNC, "arguments": {"video_key": "series/x/raw.mp4", "audio_key": "series/x/a.mp3"}}
    l = media_requests.lipsync_request("series/x/raw.mp4", "series/x/a.mp3", "latentsync", seed_from="abc")
    assert l["endpoint"] == "fal-ai/latentsync" and set(l["arguments"]) == {"video_key", "audio_key", "guidance_scale", "seed"} and l["arguments"]["guidance_scale"] == 1.0
    assert l["arguments"]["seed"] == media_requests.lipsync_request("series/x/raw.mp4", "series/x/a.mp3", "latentsync", seed_from="abc")["arguments"]["seed"]
    resolved = media_requests.resolve_keys({**l["arguments"]})
    assert {"video_url", "audio_url"} <= set(resolved) and "video_key" not in resolved                                    # keys become presigned URLs at submit time


def test_costs(env):
    assert falmedia.estimate_cost("latentsync", duration=10) == pytest.approx(0.20) and falmedia.estimate_cost("latentsync", duration=40) == pytest.approx(0.20)
    assert falmedia.estimate_cost("latentsync", duration=60) == pytest.approx(0.30)
    assert costs.shot_lipsync_cost("B2", 2, 8, False, "kling") == pytest.approx(0.14) and costs.shot_lipsync_cost("B2", 2, 8, False, "latentsync") == pytest.approx(0.20)
    assert costs.shot_lipsync_cost("A", 0, 8, False, "latentsync") == 0.0


def test_latentsync_limits():
    falmedia.check_latentsync_limits(video_seconds=8, audio_seconds=8.1)
    with pytest.raises(PermanentError, match="up to 40"):
        falmedia.check_latentsync_limits(video_seconds=41, audio_seconds=41)
    with pytest.raises(PermanentError, match="lengths must match"):
        falmedia.check_latentsync_limits(video_seconds=8, audio_seconds=5)


def lipsync_shot(sid, sync_attempts=0):
    ep = db.fetch_one("insert into episodes (series_id, number, batch, status, beat) values (%s, 1, 'pilot', 'rendered', '{}') returning id", (sid,))["id"]
    line = db.fetch_one("insert into characters (series_id, code, name, role, is_lead, visual_description) values (%s, 'CH01', 'A', 'lead', true, 'x') returning id", (sid,))["id"]
    dl = db.fetch_one("insert into dialogue_lines (series_id, episode_id, idx, character_id, text, emotion) values (%s,%s,0,%s,'hello','calm') returning id", (sid, ep, line))["id"]
    return db.fetch_one("insert into shots (series_id, episode_id, idx, shot_type, tier, spec, line_ids, status, planned_duration, render_duration, raw_clip_r2_key, sync_attempts)"
                        " values (%s,%s,0,'speaking_wider','B2','{}',%s,'rendered',4,4,%s,%s) returning *", (sid, ep, [dl], f"series/{sid}/raw.mp4", sync_attempts))


def test_the_work_item_uses_kling_first_then_latentsync_for_the_redo_and_after_a_kling_failure(env):
    sid = start(client())
    shot = lipsync_shot(sid)
    (first,) = work.lipsync_items(sid, "pilot", 0)
    assert first.endpoint == falmedia.KLING_LIPSYNC and first.estimated_cost == pytest.approx(0.014 * 5)
    db.execute("update shots set sync_attempts = 1 where id = %s", (shot["id"],))
    (redo,) = work.lipsync_items(sid, "pilot", 0)
    assert redo.endpoint == falmedia.LATENTSYNC and redo.estimated_cost == pytest.approx(0.20) and redo.request["sync_attempt"] == 1
    db.execute("update shots set sync_attempts = 0 where id = %s", (shot["id"],))
    db.execute("insert into jobs (series_id, shot_id, kind, idempotency_key, endpoint, request, status) values (%s,%s,'lipsync','k1',%s,'{}','failed')", (sid, shot["id"], falmedia.KLING_LIPSYNC))
    (after,) = work.lipsync_items(sid, "pilot", 0)
    assert after.endpoint == falmedia.LATENTSYNC and after.request["endpoint"] != first.request["endpoint"]               # a different request, so a new ledger key
    db.execute("update config set value = '\"none\"'::jsonb where key = 'lipsync_fallback'")
    from app.config import clear_config_cache

    clear_config_cache()
    (off,) = work.lipsync_items(sid, "pilot", 0)
    assert off.endpoint == falmedia.KLING_LIPSYNC


def test_the_lipsync_handler_submits_to_latentsync_with_its_own_limit(env, monkeypatch):
    from app.jobs.handlers import build_handlers as B
    from app.vendors import fixtures, storage

    sid = start(client())
    shot = lipsync_shot(sid, 1)
    storage.put_bytes(shot["raw_clip_r2_key"], fixtures.fixture_bytes("clip_4s_1080x1920.mp4"))
    for i in range(1):
        storage.put_bytes(f"series/{sid}/episodes/01/audio/line_0.mp3", fixtures.fixture_bytes("speech_2s.mp3"))
    db.execute("update dialogue_lines set audio_r2_key = %s, duration_seconds = 2.0 where series_id = %s", (f"series/{sid}/episodes/01/audio/line_0.mp3", sid))
    seen = {}
    monkeypatch.setattr(falmedia, "submit", lambda endpoint, args: seen.update(endpoint=endpoint, args=args) or "req-1")
    monkeypatch.setattr(falmedia, "check_lipsync_limits", lambda **k: (_ for _ in ()).throw(AssertionError("the Kling limits do not apply to LatentSync")))
    req = media_requests.lipsync_request(shot["raw_clip_r2_key"], B.shot_audio_key(sid, {"episode_number": 1, "idx": 0}), "latentsync", "s")
    job = {"id": "j", "series_id": sid, "shot_id": shot["id"], "endpoint": req["endpoint"], "request": {"arguments": req["arguments"]}}
    assert B.Lipsync.submit(job) == "req-1" and seen["endpoint"] == falmedia.LATENTSYNC and "video_url" in seen["args"] and "audio_url" in seen["args"]
