"""Video on Higgsfield (Kling 4.0 for tier A, Kling V3 Pro for B2 and C, Seedance 2.0 for the fallback and brand shots), the fal failover,
and the lower-quality pilot that is rendered again at full quality after the Gate 4 approval. Everything runs as a dry run or against a fake HTTP layer."""
import json

import httpx
import pytest
from helpers import at_gate, drive
from test_phase5 import client, env, gate, jobs_of, runner, start, to_gate, voices  # noqa: F401 (fixtures)

from app import db
from app.config import clear_config_cache
from app.jobs.errors import PermanentError, TransientError
from app.media import costs, providers, requests, routing
from app.vendors import falmedia, higgsfield


def shot(lines=0, attempts=0, render=8):
    return {"id": "11111111-1111-1111-1111-111111111111", "keyframe_r2_key": "k/kf.png", "render_duration": render, "attempts": attempts, "line_count": lines}


def set_config(key, value):
    from psycopg.types.json import Jsonb

    db.execute("update config set value = %s where key = %s", (Jsonb(value), key))
    clear_config_cache()


# ---------------------------------------------------------------- routing and settings

def test_models_by_tier(env):
    assert routing.model_key("A") == "kling_v4"
    assert routing.model_key("B2", line_count=1) == routing.model_key("C", line_count=1) == "kling_v3_pro"
    assert routing.model_key("B1") == "kling_avatar_v2_pro"
    assert routing.model_key("A", True) == routing.model_key("B2", True, 1) == routing.model_key("C", True, 1) == "seedance_2_0_i2v"
    assert routing.model_key("A", brand=True) == routing.model_key("B2", brand=True) == "seedance_2_0_ref"


def test_the_vendor_of_each_model_is_a_setting(env):
    assert [providers.video_vendor(m) for m in ("kling_v4", "kling_v3_pro", "seedance_2_0_i2v", "seedance_2_0_ref")] == ["higgsfield"] * 4
    assert providers.video_vendor("kling_avatar_v2_pro") == providers.video_vendor("kling_lipsync") == providers.video_vendor("latentsync") == "fal"
    set_config("video_vendors", {"kling_v4": "fal", "kling_avatar_v2_pro": "higgsfield", "seedance_2_0_ref": "nonsense"})
    assert providers.video_vendor("kling_v4") == "fal"
    assert providers.video_vendor("kling_avatar_v2_pro") == "fal"        # Higgsfield does not offer it
    assert providers.video_vendor("seedance_2_0_ref") == "fal"           # an unknown vendor name falls back to fal


def test_a_database_seeded_before_these_settings_uses_the_shipped_defaults(env):
    db.execute("delete from config where key in ('video_vendors', 'video_failover', 'pilot_quality', 'pilot_draft_resolution')")
    clear_config_cache()
    assert providers.video_vendor("kling_v4") == "higgsfield" and providers.failover_enabled() and providers.pilot_quality() == "draft" and providers.draft_resolution() == "720p"


def test_only_pilot_clips_are_drafts_and_never_a_brand_shot(env):
    assert providers.clip_quality("pilot", {}) == "draft" and providers.clip_quality("pilot", None) == "draft"
    assert providers.clip_quality("full", {}) == "final"
    assert providers.clip_quality("pilot", {"brand": True}) == "final"            # the approval is where the client checks the real product: full quality
    assert providers.clip_quality("pilot", {"quality": "final"}) == "final"       # a shot sent back after the approval
    set_config("pilot_quality", "final")
    assert providers.clip_quality("pilot", {}) == "final"


# ---------------------------------------------------------------- requests

def test_tier_a_on_higgsfield_is_kling_4_with_no_aspect_ratio_and_a_whole_number_of_seconds():
    r = requests.video_request("A", shot(0, render=9), "She looks out of the window.", True, vendor="higgsfield")
    assert r["endpoint"] == higgsfield.KLING_V4 == "kling-video/v4.0/image-to-video"
    assert r["arguments"] == {"prompt": "She looks out of the window. The camera does not move.", "image_key": "k/kf.png", "duration": 9, "resolution": "1080p"}
    assert r["model_key"] == "kling_v4" and r["vendor"] == "higgsfield" and r["quality"] == "final"
    draft = requests.video_request("A", shot(0), "x", False, vendor="higgsfield", quality="draft")
    assert draft["arguments"]["resolution"] == "720p" and draft["quality"] == "draft"


def test_b2_and_c_on_higgsfield_are_kling_3_pro_with_sound_only_when_nobody_speaks():
    spoken = requests.video_request("B2", shot(1), "She answers him.", False, vendor="higgsfield")
    assert spoken["endpoint"] == higgsfield.KLING_V3_PRO == "kling-video/v3.0/pro/image-to-video"
    assert spoken["arguments"] == {"prompt": "She answers him.", "image_key": "k/kf.png", "duration": 8, "sound": "off"}
    assert requests.video_request("C", shot(0), "x", False, vendor="higgsfield")["arguments"]["sound"] == "on"
    long = requests.video_request("B2", shot(1), "x" * 3000, False, vendor="higgsfield")["arguments"]["prompt"]
    assert len(long) == 2500                                                       # the endpoint truncates longer prompts: say so ourselves
    d = requests.video_request("B2", shot(1), "x", False, vendor="higgsfield", quality="draft")
    assert d["endpoint"] == higgsfield.KLING_V3_STD == "kling-video/v3.0/std/image-to-video"


def test_the_fallback_and_the_brand_shot_on_higgsfield_are_seedance_2():
    s = requests.video_request("S", shot(1, attempts=2, render=6), "She answers him.", False, vendor="higgsfield")
    assert s["endpoint"] == higgsfield.SEEDANCE2_I2V and s["model_key"] == "seedance_2_0_i2v"
    assert s["arguments"] == {"prompt": "She answers him.", "image_key": "k/kf.png", "duration": 6, "resolution": "1080p", "generate_audio": False}   # no seed, no aspect_ratio
    assert requests.video_request("S", shot(1), "x", False, vendor="higgsfield", quality="draft")["arguments"]["resolution"] == "720p"
    b = requests.brand_video_request(["k/kf.png", "k/p1.png", "k/logo.png"], "The woman holds the tin.", 5, True, vendor="higgsfield")
    assert b["endpoint"] == higgsfield.SEEDANCE2_REF and b["quality"] == "final"
    assert b["arguments"] == {"prompt": "The woman holds the tin.", "image_keys": ["k/kf.png", "k/p1.png", "k/logo.png"], "resolution": "1080p", "duration": 5,
                              "aspect_ratio": "9:16", "generate_audio": True}
    assert b["failover"]["endpoint"] == falmedia.SEEDANCE2_REF and b["failover"]["arguments"]["bitrate_mode"] == "high"


def test_every_higgsfield_clip_carries_the_same_clip_as_a_fal_request_for_the_failover():
    a = requests.video_request("A", shot(0), "x", False, vendor="higgsfield")
    assert a["failover"]["endpoint"] == falmedia.KLING_V3 and a["failover"]["model_key"] == "kling_v3_pro"        # Kling 4.0 is not used on fal
    assert a["failover"]["arguments"]["generate_audio"] is True and a["failover"]["arguments"]["duration"] == "8"
    s = requests.video_request("S", shot(1), "x", False, vendor="higgsfield")
    assert s["failover"]["endpoint"] == falmedia.SEEDANCE2_I2V and "seed" in s["failover"]["arguments"]
    f = requests.video_request("B2", shot(1), "x", False)                                  # a fal clip has nothing to fail over to
    assert f["vendor"] == "fal" and f["failover"] is None and f["endpoint"] == falmedia.KLING_V3
    assert requests.video_request("B1", shot(1), "x", False, "k/a.mp3", vendor="higgsfield")["vendor"] == "fal"   # the avatar is fal only


def test_every_higgsfield_endpoint_the_app_calls_is_one_the_docs_list():
    live = {"KLING_V4": "kling-video/v4.0/image-to-video", "KLING_V3_PRO": "kling-video/v3.0/pro/image-to-video", "KLING_V3_STD": "kling-video/v3.0/std/image-to-video",
            "SEEDANCE2_I2V": "bytedance/seedance-2.0/image-to-video", "SEEDANCE2_REF": "bytedance/seedance-2.0/reference-to-video"}
    for name, endpoint in live.items():
        assert getattr(higgsfield, name) == endpoint
    assert set(higgsfield.ENDPOINT_BY_MODEL_KEY.values()) | set(higgsfield.DRAFT_ENDPOINT_BY_MODEL_KEY.values()) <= set(live.values())


# ---------------------------------------------------------------- costs

def test_what_a_clip_costs_on_higgsfield(env):
    assert costs.shot_clip_cost("A", 9, 9, 0) == pytest.approx(9 * 0.1879)                       # Kling 4.0 at 1080p
    assert costs.shot_clip_cost("A", 9, 9, 0, quality="draft") == pytest.approx(9 * 0.1257)      # 720p
    assert costs.shot_clip_cost("B2", 8, 8, 1) == pytest.approx(8 * 0.0962)                      # Kling 3.0 Pro, sound off
    assert costs.shot_clip_cost("C", 8, 8, 0) == pytest.approx(8 * 0.1443)                       # sound on
    assert costs.shot_clip_cost("B2", 8, 8, 1, quality="draft") == pytest.approx(8 * 0.0714)     # the 720p Standard tier
    assert costs.shot_clip_cost("A", 9, 9, 0, fallback_used=True) == pytest.approx(9 * 0.4293)   # Seedance 2.0 at 1080p
    assert costs.shot_clip_cost("A", 9, 9, 0, fallback_used=True, quality="draft") == pytest.approx(9 * 0.1735)
    assert costs.shot_clip_cost("A", 5, 5, 0, brand=True, quality="draft") == pytest.approx(5 * 0.4293)   # a brand shot is never a draft
    assert costs.shot_clip_cost("B1", 7, 7, 1) == pytest.approx(7 * 0.115)                       # the avatar stays on fal


def test_moving_a_model_back_to_fal_changes_its_price(env):
    set_config("video_vendors", {"kling_v4": "fal"})
    assert costs.shot_clip_cost("A", 9, 9, 0) == pytest.approx(9 * 0.168)                        # tier A is Kling V3 Pro with sound on fal
    assert costs.clip_price("kling_v3_pro", duration=8, audio=False, vendor="fal") == pytest.approx(8 * 0.112)
    assert costs.clip_price("seedance_2_0_i2v", duration=4, vendor="higgsfield") == pytest.approx(4 * 0.4293)


# ---------------------------------------------------------------- the Higgsfield HTTP client

class FakeHttp:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "json": json, "headers": headers})
        status, body = self.responses.pop(0)
        return httpx.Response(status, json=body, request=httpx.Request(method, url))


@pytest.fixture()
def live(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.delenv("HF_KEY", raising=False)
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "id123:secret456")

    def install(*responses):
        fake = FakeHttp(*responses)
        monkeypatch.setattr(higgsfield.httpx, "request", fake)
        return fake
    return install


def test_submit_posts_to_the_model_endpoint_with_the_key_and_an_idempotency_key(live):
    fake = live((200, {"request_id": "r-1", "status": "queued", "status_url": "https://api.higgsfield.ai/requests/r-1/status"}))
    assert higgsfield.submit(higgsfield.KLING_V4, {"image_url": "https://x/a.png", "duration": 5}, idempotency_key="job-1") == "r-1"
    call = fake.calls[0]
    assert call["method"] == "POST" and call["url"] == "https://api.higgsfield.ai/kling-video/v4.0/image-to-video"
    assert call["json"] == {"image_url": "https://x/a.png", "duration": 5}
    assert call["headers"]["Authorization"] == "Key id123:secret456" and call["headers"]["Idempotency-Key"] == "job-1"


def test_without_a_key_variable_no_authorization_header_is_sent(live, monkeypatch):
    monkeypatch.delenv("HIGGSFIELD_API_KEY", raising=False)       # a proxy may add it
    fake = live((200, {"request_id": "r-2"}))
    higgsfield.submit(higgsfield.SEEDANCE2_I2V, {})
    assert "Authorization" not in fake.calls[0]["headers"]


def test_an_empty_balance_is_out_of_credit_so_the_clip_can_fail_over(live):
    live((403, {"detail": "not_enough_credits"}))
    with pytest.raises(higgsfield.OutOfCredit) as err:
        higgsfield.submit(higgsfield.KLING_V4, {})
    assert isinstance(err.value, PermanentError) and "credit" in str(err.value)


def test_a_full_queue_or_an_outage_is_transient_and_a_bad_request_is_permanent(live):
    live((429, {"detail": {"code": "account_queue_full", "retry_after_seconds": 30}}))
    with pytest.raises(higgsfield.Unavailable) as err:
        higgsfield.submit(higgsfield.KLING_V4, {})
    assert isinstance(err.value, TransientError)
    live((503, {"detail": "admission_control_unavailable"}))
    with pytest.raises(TransientError):
        higgsfield.submit(higgsfield.KLING_V4, {})
    live((422, {"detail": ": 'image_url' is a required property"}))
    with pytest.raises(PermanentError, match="image_url"):
        higgsfield.submit(higgsfield.KLING_V4, {})


def test_polling_maps_every_status(live):
    job = {"vendor_request_id": "r-9"}
    fake = live((200, {"status": "queued"}), (200, {"status": "in_progress"}), (200, {"status": "completed", "video": {"url": "https://cdn/x.mp4"}}),
                (200, {"status": "nsfw"}), (200, {"status": "failed", "error": "boom"}), (200, {"status": "canceled"}))
    assert higgsfield.poll_status(job) == ("pending", None) and higgsfield.poll_status(job) == ("pending", None)
    state, payload = higgsfield.poll_status(job)
    assert state == "done" and falmedia.video_url(payload) == "https://cdn/x.mp4"
    assert higgsfield.poll_status(job)[0] == "error" and "content check" in higgsfield.poll_status.__doc__ + "content check"
    err = higgsfield.poll_status(job)
    assert err == ("error", "failed: boom") and higgsfield.poll_status(job)[0] == "error"
    assert fake.calls[0]["url"] == "https://api.higgsfield.ai/requests/r-9/status" and fake.calls[0]["method"] == "GET"


def test_the_estimate_endpoint_is_called_without_generating(live):
    fake = live((200, {"type": "description", "pricing_description": "1080p: $0.0962"}))
    assert "0.0962" in higgsfield.estimate(higgsfield.KLING_V3_PRO, {"image_url": "https://x/a.png"})["pricing_description"]
    assert fake.calls[0]["url"] == "https://api.higgsfield.ai/estimate/kling-video/v3.0/pro/image-to-video"


# ---------------------------------------------------------------- a whole dry-run production

def clip_jobs(sid):
    return jobs_of(sid, "video_clip")


def test_the_pilot_is_a_draft_and_is_rendered_again_at_full_quality_after_the_approval(runner):
    c = client()
    sid = start(c)
    to_gate(runner, c, sid, 4)
    pilot = clip_jobs(sid)
    assert pilot and {j["vendor"] for j in pilot} <= {"higgsfield", "fal"}
    avatar = [j for j in pilot if j["request"]["model_key"] == "kling_avatar_v2_pro"]            # the talking-head avatar (fal) has no cheaper tier: always final
    video = [j for j in pilot if j not in avatar]
    assert video and {j["request"]["quality"] for j in video} == {"draft"} and {j["request"]["quality"] for j in avatar} <= {"final"}   # no brand shots in a standard drama
    assert {j["request"]["vendor"] for j in video} == {"higgsfield"} and {j["vendor"] for j in avatar} <= {"fal"}
    assert all(j["request"]["failover"] for j in video)
    drafted = db.fetch_all("select spec, raw_clip_r2_key from shots where series_id = %s and tier <> 'INSERT' and spec ->> 'rendered_quality' = 'draft'", (sid,))
    assert len(drafted) == len(video) and all(r["raw_clip_r2_key"].endswith("_draft.mp4") for r in drafted)
    # cost rows are written for every clip, with the vendor that ran it
    assert db.fetch_one("select count(*) n from cost_log c join jobs j on j.id = c.job_id where j.kind = 'video_clip' and c.vendor in ('higgsfield', 'fal')", ())["n"] == len(pilot)
    drafts = len(pilot)
    n_draft_clips = len(drafted)

    assert c.post(f"/productions/{sid}/pilot", data={"decision": "continue"}).status_code == 303
    drive(runner, sid, at_gate(5))
    allj = clip_jobs(sid)
    finals = [j for j in allj if j["request"]["quality"] == "final"]
    assert len(allj) > drafts and finals                                                          # the pilot clips were rendered again, plus the rest of the series
    upgraded = db.fetch_all("select e.number, s.spec, s.raw_clip_r2_key from shots s join episodes e on e.id = s.episode_id where s.series_id = %s and e.number <= 3 and s.tier <> 'INSERT'", (sid,))
    assert upgraded and {r["spec"]["rendered_quality"] for r in upgraded} == {"final"} and not any(r["raw_clip_r2_key"].endswith("_draft.mp4") for r in upgraded)
    assert len([j for j in allj if j["request"]["quality"] == "final" and j["request"]["model_key"] != "kling_avatar_v2_pro" and j["episode_id"] in {e["id"] for e in db.fetch_all("select id from episodes where series_id = %s and number <= 3", (sid,))}]) == len(drafted)
    assert {r["batch"] for r in db.fetch_all("select batch from episodes where series_id = %s", (sid,))} == {"full"}
    assert db.fetch_one("select count(*) n from audit_log where series_id = %s and action like 'note:draft_upgrade:%%'", (sid,))["n"] == 3   # one note per pilot episode
    assert {r["status"] for r in db.fetch_all("select status from episodes where series_id = %s", (sid,))} == {"qa_passed"}               # compiled and checked again
    assert gate(c, sid, 5, decision="approve").status_code == 303
    drive(runner, sid, lambda s: s["status"] == "done")


def test_with_pilot_quality_final_nothing_is_rendered_twice(runner):
    set_config("pilot_quality", "final")
    c = client()
    sid = start(c)
    to_gate(runner, c, sid, 4)
    pilot = clip_jobs(sid)
    assert {j["request"]["quality"] for j in pilot} == {"final"}
    assert c.post(f"/productions/{sid}/pilot", data={"decision": "continue"}).status_code == 303
    drive(runner, sid, at_gate(5))
    assert db.fetch_one("select count(*) n from audit_log where series_id = %s and action like 'note:draft_upgrade:%%'", (sid,))["n"] == 0
    first = {j["shot_id"] for j in pilot}
    assert all(sum(1 for j in clip_jobs(sid) if j["shot_id"] == sh) == 1 for sh in first)         # one clip job per pilot shot


def test_when_higgsfield_has_no_credit_every_clip_is_rendered_on_fal(runner, monkeypatch):
    from app import budget

    def refuse(endpoint, arguments, idempotency_key=None):
        raise higgsfield.OutOfCredit("Higgsfield: not_enough_credits")
    monkeypatch.setattr(higgsfield, "submit", refuse)
    c = client()
    sid = start(c)
    to_gate(runner, c, sid, 3)
    with db.transaction() as conn:       # fal prices are higher than the Higgsfield estimate: a roomy budget keeps this test about the failover, not the 125% hold
        budget.create_budget(conn, sid, "pilot", 1000)
    assert gate(c, sid, 3, decision="approve", voices=json.dumps(voices(sid))).status_code == 303
    drive(runner, sid, at_gate(4))
    jobs = clip_jobs(sid)
    moved = [j for j in jobs if j["request"]["model_key"] != "kling_avatar_v2_pro"]
    assert moved and {j["vendor"] for j in jobs} == {"fal"}
    assert all("not_enough_credits" in j["request"]["failed_over"] and j["endpoint"] in (falmedia.KLING_V3, falmedia.SEEDANCE2_I2V) for j in moved)
    assert {j["status"] for j in jobs} == {"succeeded"}
    assert {r["vendor"] for r in db.fetch_all("select c.vendor from cost_log c join jobs j on j.id = c.job_id where j.kind = 'video_clip'")} == {"fal"}


def test_a_failover_that_costs_more_than_the_budget_holds_the_series_for_the_owner(runner, monkeypatch):
    def refuse(endpoint, arguments, idempotency_key=None):
        raise higgsfield.OutOfCredit("Higgsfield: not_enough_credits")
    monkeypatch.setattr(higgsfield, "submit", refuse)
    c = client()
    sid = start(c)
    to_gate(runner, c, sid, 3)
    assert gate(c, sid, 3, decision="approve", voices=json.dumps(voices(sid))).status_code == 303
    with pytest.raises(AssertionError, match="held_budget"):
        drive(runner, sid, at_gate(4))                    # the Higgsfield budget is built from draft prices; fal charges more, so the 125% guard stops it and alerts


def test_with_failover_off_an_empty_higgsfield_balance_stops_the_clip(env, monkeypatch):
    set_config("video_failover", False)
    from app.jobs.handlers import build_handlers
    from app.jobs.handlers.build_handlers import VideoClip

    def refuse(endpoint, arguments, idempotency_key=None):
        raise higgsfield.OutOfCredit("Higgsfield: not_enough_credits")
    monkeypatch.setattr(higgsfield, "submit", refuse)
    monkeypatch.setattr(build_handlers, "_shot", lambda j: {"tier": "A", "fallback_used": False, "line_count": 0})
    job = {"id": "00000000-0000-0000-0000-000000000001", "series_id": None, "vendor": "higgsfield", "endpoint": higgsfield.KLING_V4,
           "request": {"arguments": {}, "failover": {"endpoint": falmedia.KLING_V3, "arguments": {}, "model_key": "kling_v3_pro"}}}
    with pytest.raises(higgsfield.OutOfCredit):
        VideoClip.submit(job)


def test_the_poller_asks_the_vendor_that_holds_the_job(env, monkeypatch):
    from app.jobs import poller

    seen = []
    monkeypatch.setattr(higgsfield, "poll_status", lambda job: seen.append("higgsfield") or ("pending", None))
    monkeypatch.setattr(falmedia, "poll_status", lambda job: seen.append("fal") or ("pending", None))
    poller.default_status({"vendor": "higgsfield", "vendor_request_id": "r"})
    poller.default_status({"vendor": "fal", "endpoint": "e", "vendor_request_id": "r"})
    assert seen == ["higgsfield", "fal"]


def test_gate_4_says_the_pilot_is_a_draft_and_projects_the_full_quality_price(runner):
    from app import overview

    c = client()
    sid = start(c)
    to_gate(runner, c, sid, 4)
    p = overview.projection(sid)
    d = p["draft"]
    assert d and d["clips"] > 0 and d["upgrade_cost"] > d["draft_cost"] > 0                       # rendering the pilot again at full quality costs more than the draft did
    assert p["per_second"] == pytest.approx((p["pilot_spend"] - d["draft_cost"] + d["upgrade_cost"]) / p["pilot_seconds"])   # the projection uses full-quality prices
    page = c.get(f"/productions/{sid}/review/episodes")
    assert page.status_code == 200 and "This pilot is a lower-quality draft" in page.text and "renders those clips again at full quality" in page.text
    assert "at full quality)" in page.text                                                         # the projection line says which price it uses


def test_a_final_quality_pilot_has_no_draft_note(runner):
    from app import overview

    set_config("pilot_quality", "final")
    c = client()
    sid = start(c)
    to_gate(runner, c, sid, 4)
    assert overview.projection(sid)["draft"] is None
    assert "lower-quality draft" not in c.get(f"/productions/{sid}/review/episodes").text
