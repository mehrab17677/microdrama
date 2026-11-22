import base64
import hashlib
import io
import json
import time
import uuid
from typing import Optional

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import BaseModel

from app import db
from app.config import clear_config_cache
from app.jobs import ledger, queue
from app.jobs.errors import PermanentError, TransientError
from app.vendors import eleven, errors, falmedia, falwebhook, fixtures, images, llm
from app.web.main import app
from scripts import migrate, seed_config


@pytest.fixture()
def env(db_url):
    migrate.apply_migrations(db_url)
    with db.connect(db_url) as conn:
        conn.execute("truncate users, audit_log, alerts, series, jobs, cost_log, graph_commands, config cascade")
        conn.commit()
    seed_config.seed(db_url)
    clear_config_cache()
    falwebhook.clear_cache()
    yield
    clear_config_cache()


# ---------------- language models ----------------

class Inner(BaseModel):
    name: str
    tags: list[str]


class Sample(BaseModel):
    title: str
    count: int
    ratio: float
    flag: bool
    inner: Inner
    items: list[Inner]
    note: Optional[str] = None
    mapping: dict[str, str]


def test_llm_dry_run_returns_schema_valid_objects(env):
    value, usage = llm.complete("story", "sys", [{"role": "user", "content": "hi"}], schema=Sample)
    assert isinstance(value, Sample) and value.items and value.inner.tags
    assert usage["model"] == "anthropic:claude-sonnet-5-5" and usage["cost_usd"] == 0
    text, _ = llm.complete("copy", "sys", [{"role": "user", "content": "hi"}])
    assert isinstance(text, str) and text


def test_llm_unknown_role_is_permanent(env):
    with pytest.raises(PermanentError):
        llm.complete("nonsense", "s", [])


def test_llm_prices_from_the_price_book(env):
    assert llm.llm_cost("anthropic:claude-sonnet-5-5", 1_000_000, 1_000_000) == pytest.approx(12.0)
    assert llm.llm_cost("anthropic:claude-opus-5-5", 1_000_000, 1_000_000) == pytest.approx(24.0)
    assert llm.llm_cost("anthropic:claude-haiku-5-5", 100_000, 0) == pytest.approx(0.01)
    assert llm.llm_cost("anthropic:claude-haiku-5-5", 200_000, 0) == pytest.approx(0.10)  # long-prompt rate
    assert llm.llm_cost("openai:gpt-6-luna", 1_000_000, 1_000_000) == pytest.approx(0.6)


def test_every_configured_role_has_a_price(env):
    from app.config import get_config

    for role, model in get_config("llm_roles").items():
        assert model in get_config("price_book")["llm"], role


# ---------------- images and embeddings ----------------

def png(size):
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 20, 30)).save(buf, "PNG")
    return buf.getvalue()


def test_image_dry_run_is_1080x1920(env):
    data, usage = images.generate_image("a portrait")
    assert Image.open(io.BytesIO(data)).size == (1080, 1920)
    assert usage["size"] == "1152x2048"  # default mode: exact 9:16, divisible by 16


def test_image_reference_limit(env):
    with pytest.raises(PermanentError):
        images.generate_image("x", references=[b"1"] * 17)
    images.generate_image("x", references=[b"1"] * 16)


@pytest.mark.parametrize("mode,src,req", [
    ("scale_1152x2048", (1152, 2048), "1152x2048"),
    ("crop_1024x1536", (1024, 1536), "1024x1536"),
    ("native_1080x1920", (1080, 1920), "1080x1920"),
])
def test_size_modes(env, mode, src, req):
    assert images.request_size(mode) == req
    assert Image.open(io.BytesIO(images.to_target_size(png(src), mode))).size == (1080, 1920)
    assert all(int(v) % 16 == 0 for v in images.request_size("scale_1152x2048").split("x"))


def test_crop_mode_removes_the_sides_not_the_top(env):
    img = Image.new("RGB", (1024, 1536), (0, 0, 0))
    img.paste((255, 0, 0), (0, 0, 80, 1536))      # left edge, to be cropped away
    img.paste((0, 255, 0), (472, 0, 552, 1536))  # centre strip, kept
    buf = io.BytesIO()
    img.save(buf, "PNG")
    out = Image.open(io.BytesIO(images.to_target_size(buf.getvalue(), "crop_1024x1536")))
    assert out.getpixel((540, 960))[1] > 200 and out.getpixel((2, 960))[0] < 50


def test_image_cost_uses_usage_details(env):
    usage = {"input_tokens_details": {"text_tokens": 1000, "image_tokens": 2000}, "output_tokens": 6000}
    assert images.image_cost(usage) == pytest.approx(1000 / 1e6 * 5 + 2000 / 1e6 * 8 + 6000 / 1e6 * 30)


def test_embeddings_dry_run(env):
    vecs, usage = images.embed(["alpha", "beta", "alpha"])
    assert [len(v) for v in vecs] == [1536] * 3 and vecs[0] == vecs[2] and vecs[0] != vecs[1]
    assert sum(x * x for x in vecs[0]) == pytest.approx(1.0)
    assert images.embed([]) == ([], {"model": "text-embedding-3-small", "vendor": "fal", "tokens": 0, "cost_usd": 0.0})


# ---------------- ElevenLabs ----------------

def test_word_timings_skip_audio_tags_and_split_on_space():
    text = "[angry] Go away now"
    chars = list(text)
    al = {"characters": chars, "character_start_times_seconds": [i * 0.1 for i in range(len(chars))],
          "character_end_times_seconds": [i * 0.1 + 0.1 for i in range(len(chars))]}
    words = eleven.word_timings(al)
    assert [w["w"] for w in words] == ["Go", "away", "now"]
    assert words[0]["start"] == pytest.approx(0.8) and words[0]["end"] == pytest.approx(1.0)
    assert words[2]["end"] == pytest.approx(len(chars) * 0.1)


def test_word_timings_bangla_and_null():
    text = "আমি তোমাকে ভালোবাসি"
    n = len(text)
    al = {"characters": list(text), "character_start_times_seconds": [i / 10 for i in range(n)],
          "character_end_times_seconds": [(i + 1) / 10 for i in range(n)]}
    assert [w["w"] for w in eleven.word_timings(al)] == ["আমি", "তোমাকে", "ভালোবাসি"]
    assert eleven.word_timings(None) == []


def test_pronunciations_replace_longest_term_first():
    assert eleven.apply_pronunciations("Dr. Rahman met Rahman", [("Rahman", "Rahmaan"), ("Dr. Rahman", "Doctor Rahmaan")]) == "Doctor Rahmaan met Rahmaan"


def test_stable_seed_is_stable_and_in_range():
    a = eleven.stable_seed("line-1", 1)
    assert a == eleven.stable_seed("line-1", 1) != eleven.stable_seed("line-1", 2)
    assert 0 <= a <= 4294967295


def test_speech_dry_run_and_limits(env):
    out, usage = eleven.speech("[calm] Hello there", "voice1", "en", 7)
    assert out["audio"][:3] in (b"ID3", b"\xff\xfb", b"\xff\xf3") or out["audio"]
    assert [w["w"] for w in out["words"]] == ["Hello", "there"]
    assert usage["cost_usd"] == pytest.approx(len("[calm] Hello there") / 1000 * 0.08)
    with pytest.raises(PermanentError):
        eleven.speech("x" * 2001, "v", "en", 1)


def test_music_sfx_voice_and_stt_dry_run(env):
    audio, usage = eleven.music("Tense instrumental, no vocals", 90_000)
    assert audio and usage["cost_usd"] == pytest.approx(0.225)
    for bad in (2999, 600_001):
        with pytest.raises(PermanentError):
            eleven.music("x", bad)
    assert eleven.sound_effect("rain", 30, loop=True)[0]
    with pytest.raises(PermanentError):
        eleven.sound_effect("rain", 31)
    previews, _ = eleven.design_voice("A warm low female voice, Bangladeshi accent")
    assert previews[0]["generated_voice_id"].startswith("dry-gen-")
    voice_id = eleven.save_voice(previews[0]["generated_voice_id"], "CH01", "desc")
    assert voice_id.startswith("dry-voice-") and voice_id == eleven.save_voice(previews[0]["generated_voice_id"], "CH01", "desc")
    assert eleven.transcribe(b"audio", "bn")[0] == "fixture transcript"


# ---------------- fal media ----------------

def test_price_formulas_match_the_guide(env):
    assert falmedia.estimate_cost("seedance_2_0_i2v", duration=4) == pytest.approx(2.728)                    # Seedance 2.0 at 1080p, $0.682 per second, audio included
    with pytest.raises(PermanentError):
        falmedia.estimate_cost("seedance_1_5_pro", duration=4)                                              # removed from fal on 11 Nov 2026
    assert falmedia.estimate_cost("kling_v3_pro", duration=10, audio=True) == pytest.approx(1.68)
    assert falmedia.estimate_cost("kling_v3_pro", duration=10, audio=False) == pytest.approx(1.12)
    assert falmedia.estimate_cost("kling_avatar_v2_pro", duration=5) == pytest.approx(0.575)
    assert falmedia.estimate_cost("kling_lipsync", duration=6) == pytest.approx(0.14)   # billed in 5 s steps
    assert falmedia.estimate_cost("kling_lipsync", duration=5) == pytest.approx(0.07)
    with pytest.raises(PermanentError):
        falmedia.estimate_cost("nope", duration=1)


def test_lipsync_limits_are_checked_locally():
    ok = dict(video_seconds=5, video_bytes=10_000_000, width=1080, height=1920, audio_seconds=5, audio_bytes=100_000)
    falmedia.check_lipsync_limits(**ok)
    for patch in ({"video_seconds": 1.5}, {"video_seconds": 10.5}, {"width": 640}, {"audio_seconds": 1}, {"audio_bytes": 6_000_000}, {"video_bytes": 101 * 1024 * 1024}):
        with pytest.raises(PermanentError):
            falmedia.check_lipsync_limits(**{**ok, **patch})


def test_fal_dry_run_submit_poll_and_download(env, tmp_path):
    rid = falmedia.submit(falmedia.KLING_V3, {"prompt": "x"})
    assert rid.startswith("dry-")
    state, payload = falmedia.poll_status({"endpoint": falmedia.KLING_V3, "vendor_request_id": rid})
    assert state == "done"
    out = falmedia.download(falmedia.video_url(payload), tmp_path / "clip.mp4")
    assert out.read_bytes() == fixtures.fixture_bytes("clip_4s_1080x1920.mp4")
    with pytest.raises(PermanentError):
        falmedia.video_url({"nope": 1})


def test_webhook_url_only_with_a_public_base(env, monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    assert falmedia.webhook_url() is None
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.com/")
    assert falmedia.webhook_url() == "https://example.com/webhooks/fal"


# ---------------- error mapping ----------------

def test_error_classification():
    assert errors.classify_status(200) is None
    assert isinstance(errors.classify_status(429, "slow down"), TransientError)
    assert isinstance(errors.classify_status(429, "You have no credits remaining"), PermanentError)
    assert isinstance(errors.classify_status(503), TransientError)
    assert isinstance(errors.classify_status(422, "bad"), PermanentError)
    assert isinstance(errors.map_exception(httpx.ConnectError("x")), TransientError)
    assert isinstance(errors.map_exception(ValueError("x")), PermanentError)


# ---------------- webhook ----------------

KEY = Ed25519PrivateKey.generate()
X = base64.urlsafe_b64encode(KEY.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).rstrip(b"=").decode()


@pytest.fixture()
def hook(env, monkeypatch):
    fetches = []
    monkeypatch.setattr(falwebhook, "fetch_jwks", lambda: fetches.append(1) or [X])
    return fetches


def signed(body: dict, request_id="req-1", ts=None, key=KEY):
    raw = json.dumps(body).encode()
    ts = str(int(time.time()) if ts is None else ts)
    sig = key.sign(falwebhook.signed_message(request_id, "user-1", ts, raw)).hex()
    return raw, {
        "X-Fal-Webhook-Request-Id": request_id, "X-Fal-Webhook-User-Id": "user-1",
        "X-Fal-Webhook-Timestamp": ts, "X-Fal-Webhook-Signature": sig, "Content-Type": "application/json",
    }


def post(raw, headers):
    return TestClient(app, base_url="https://testserver").post("/webhooks/fal", content=raw, headers=headers)


def submitted_job(request_id="req-1"):
    owner = db.fetch_one("insert into users (email, display_name, password_hash, role, status) values ('o@x.io','o','h','member','active') returning id")["id"]
    sid = uuid.uuid4()
    db.execute("insert into series (id, owner_id, title, category, language, status, current_stage, thread_id) values (%s,%s,'t','standard','bn','waiting_jobs','build',%s)", (sid, owner, str(sid)))
    j = ledger.enqueue("video_clip", series_id=sid, vendor="fal", endpoint=falmedia.KLING_V3, request={"k": 1})
    queue.claim("w")
    assert ledger.mark_submitted(j["id"], request_id)
    return j["id"]


def status(job_id):
    return ledger.get_job(job_id)


def test_valid_delivery_moves_the_job_to_finalize(hook):
    jid = submitted_job()
    body = {"request_id": "req-1", "gateway_request_id": "g", "status": "OK", "payload": {"video": {"url": "https://v3.fal.media/x.mp4"}, "seed": 5}}
    r = post(*signed(body))
    assert r.status_code == 200 and r.json()["result"] == "applied"
    row = status(jid)
    assert row["status"] == "queued" and row["phase"] == "finalize" and row["result"]["seed"] == 5


def test_repeated_delivery_is_ignored(hook):
    jid = submitted_job()
    body = {"request_id": "req-1", "status": "OK", "payload": {"video": {"url": "u"}}}
    assert post(*signed(body)).json()["result"] == "applied"
    first = status(jid)
    r = post(*signed({**body, "payload": {"video": {"url": "DIFFERENT"}}}))
    assert r.status_code == 200 and r.json()["result"] == "ignored"
    assert status(jid)["result"] == first["result"]


def test_bad_signature_is_rejected(hook):
    jid = submitted_job()
    raw, headers = signed({"request_id": "req-1", "status": "OK", "payload": {}}, key=Ed25519PrivateKey.generate())
    assert post(raw, headers).status_code == 401
    raw, headers = signed({"request_id": "req-1", "status": "OK", "payload": {}})
    assert post(raw + b" ", headers).status_code == 401  # body changed after signing
    assert status(jid)["status"] == "submitted"


def test_stale_timestamp_is_rejected(hook):
    jid = submitted_job()
    assert post(*signed({"request_id": "req-1", "status": "OK", "payload": {}}, ts=int(time.time()) - 301)).status_code == 401
    assert post(*signed({"request_id": "req-1", "status": "OK", "payload": {}}, ts=int(time.time()) + 301)).status_code == 401
    assert post(*signed({"request_id": "req-1", "status": "OK", "payload": {}}, ts=int(time.time()) - 290)).status_code == 200
    assert status(jid)["phase"] == "finalize"


@pytest.mark.parametrize("missing", list(falwebhook.REQUIRED_HEADERS))
def test_missing_header_is_rejected(hook, missing):
    raw, headers = signed({"request_id": "req-1", "status": "OK", "payload": {}})
    headers.pop(missing)
    r = post(raw, headers)
    assert r.status_code == 401 and missing in r.json()["detail"]


def test_error_delivery_fails_the_job(hook):
    jid = submitted_job()
    assert post(*signed({"request_id": "req-1", "status": "ERROR", "error": "content_policy_violation"})).status_code == 200
    row = status(jid)
    assert row["status"] == "failed" and "content_policy_violation" in row["error"]


def test_payload_error_counts_as_failure(hook):
    jid = submitted_job()
    post(*signed({"request_id": "req-1", "status": "OK", "payload": None, "payload_error": "too large"}))
    assert status(jid)["status"] == "failed"


def test_unknown_request_gets_a_retryable_status(hook):
    assert post(*signed({"request_id": "nope", "status": "OK", "payload": {}}, request_id="nope")).status_code == 404


def test_non_json_body_is_a_400(hook):
    raw = b"not json"
    ts = str(int(time.time()))
    sig = KEY.sign(falwebhook.signed_message("r", "u", ts, raw)).hex()
    r = post(raw, {"X-Fal-Webhook-Request-Id": "r", "X-Fal-Webhook-User-Id": "u", "X-Fal-Webhook-Timestamp": ts, "X-Fal-Webhook-Signature": sig})
    assert r.status_code == 400


def test_jwks_is_cached_and_refreshed_on_rotation(hook, monkeypatch):
    submitted_job()
    post(*signed({"request_id": "req-1", "status": "OK", "payload": {}}))
    post(*signed({"request_id": "req-1", "status": "OK", "payload": {}}))
    assert len(hook) == 1                      # cached across deliveries
    new = Ed25519PrivateKey.generate()
    newx = base64.urlsafe_b64encode(new.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).rstrip(b"=").decode()
    monkeypatch.setattr(falwebhook, "fetch_jwks", lambda: hook.append(1) or [newx])
    assert post(*signed({"request_id": "req-1", "status": "OK", "payload": {}}, key=new)).status_code == 200  # rotation: refetched
    assert len(hook) == 2


def test_cache_expires_after_24_hours(hook, monkeypatch):
    falwebhook.get_keys()
    t0 = falwebhook._cache[0]
    monkeypatch.setattr(time, "time", lambda: t0 + 24 * 3600 + 1)
    falwebhook.get_keys()
    assert len(hook) == 2


# ---------------- fixtures ----------------

def test_synthesized_fixture_for_unknown_schema_prefers_a_file(env, tmp_path, monkeypatch):
    monkeypatch.setattr(fixtures, "FIXTURE_DIR", tmp_path)
    (tmp_path / "llm").mkdir()
    (tmp_path / "llm" / "Inner.json").write_text(json.dumps({"name": "from file", "tags": ["a", "b"]}))
    assert fixtures.synthesize(Inner).name == "from file"
    assert fixtures.synthesize(Sample).inner.name.startswith("fixture")
