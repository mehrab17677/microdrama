"""Video models after Seedance 1.5 Pro was removed from fal (11 Nov 2026): Kling V3 Pro for tiers A, B2 and C, Seedance 2.0 image-to-video as the fallback."""
import pytest
from test_phase5 import env  # noqa: F401 (fixture)

from app import db
from app.config import clear_config_cache
from app.media import costs, durations, requests, routing
from app.vendors import falmedia


def shot(lines=0, attempts=0, render=8):
    return {"id": "11111111-1111-1111-1111-111111111111", "keyframe_r2_key": "k/kf.png", "render_duration": render, "attempts": attempts, "line_count": lines}


def test_every_fal_endpoint_the_app_calls_is_a_known_live_one():
    live = {
        "KLING_V3": "fal-ai/kling-video/v3/pro/image-to-video", "KLING_AVATAR": "fal-ai/kling-video/ai-avatar/v2/pro", "KLING_LIPSYNC": "fal-ai/kling-video/lipsync/audio-to-video",
        "LATENTSYNC": "fal-ai/latentsync", "SEEDANCE2_REF": "bytedance/seedance-2.0/reference-to-video", "SEEDANCE2_I2V": "bytedance/seedance-2.0/image-to-video"}
    for name, endpoint in live.items():
        assert getattr(falmedia, name) == endpoint, name
    assert not hasattr(falmedia, "SEEDANCE")
    assert not any("seedance/v1.5" in v for v in vars(falmedia).values() if isinstance(v, str))             # removed from fal on 11 Nov 2026
    assert set(falmedia.ENDPOINT_BY_MODEL_KEY.values()) <= set(live.values())


def test_tiers_a_b2_and_c_use_kling_v3_and_every_fallback_uses_seedance_2():
    assert routing.model_key("A") == routing.model_key("B2") == routing.model_key("C") == "kling_v3_pro" and routing.model_key("B1") == "kling_avatar_v2_pro"
    assert [routing.method_tier(t, True, 1) for t in ("A", "B1", "B2", "C")] == ["S", "B2", "S", "S"]
    assert routing.model_key("A", True) == routing.model_key("B2", True, 1) == routing.model_key("C", True, 1) == "seedance_2_0_i2v"
    assert routing.model_key("B1", True, 1) == "kling_v3_pro"                                                # the avatar's fallback is a silent Kling clip, then lip-sync
    assert routing.needs_lipsync("A", 1, True) and not routing.needs_lipsync("A", 0, True) and routing.needs_lipsync("B2", 2) and routing.needs_lipsync("C", 1)


def test_kling_requests_carry_audio_only_for_shots_with_no_speech_and_lock_the_camera_in_words():
    silent = requests.video_request("A", shot(0), "She looks out of the window.", True)
    assert silent["endpoint"] == falmedia.KLING_V3
    assert silent["arguments"] == {"prompt": "She looks out of the window. The camera does not move.", "start_image_key": "k/kf.png", "duration": "8", "generate_audio": True}
    spoken = requests.video_request("B2", shot(1), "She answers him.", False)
    assert spoken["arguments"]["generate_audio"] is False and spoken["arguments"]["prompt"] == "She answers him." and "camera_fixed" not in spoken["arguments"]
    assert requests.video_request("C", shot(1), "x", True)["arguments"]["generate_audio"] is False
    already = requests.video_request("A", shot(0), "The camera holds still on her face.", True)["arguments"]["prompt"]
    assert already == "The camera holds still on her face."                                                   # not said twice


def test_the_fallback_request_is_seedance_2_image_to_video_at_1080p_9_16():
    r = requests.video_request("S", shot(1, attempts=2, render=6), "She answers him.", False)
    a = r["arguments"]
    assert r["endpoint"] == falmedia.SEEDANCE2_I2V
    assert a["image_key"] == "k/kf.png" and a["resolution"] == "1080p" and a["aspect_ratio"] == "9:16" and a["duration"] == "6" and a["generate_audio"] is False
    assert isinstance(a["seed"], int) and "camera_fixed" not in a
    assert requests.video_request("S", shot(0), "x", False)["arguments"]["generate_audio"] is True


def test_seedance_2_needs_at_least_four_seconds_like_the_other_main_methods():
    assert durations.render_seconds("S", 1.2) == 4 and durations.render_seconds("S", 12.0) == 10 and durations.render_seconds("A", 2.0) == 4 and durations.render_seconds("C", 1.0) == 3


def test_what_each_method_costs_per_shot(env):
    assert costs.shot_clip_cost("A", 9, 9, 0) == pytest.approx(9 * 0.168)                                     # Kling V3 with audio
    assert costs.shot_clip_cost("B2", 8, 8, 1) == pytest.approx(8 * 0.112) and costs.shot_lipsync_cost("B2", 1, 8) == pytest.approx(0.14)
    assert costs.shot_clip_cost("C", 9.5, 10, 1) == pytest.approx(10 * 0.112)
    assert costs.shot_clip_cost("B1", 7, 7, 1) == pytest.approx(7 * 0.115)
    assert costs.shot_clip_cost("A", 9, 9, 0, fallback_used=True) == pytest.approx(9 * 0.682)                  # the fallback is the expensive route
    assert costs.shot_clip_cost("A", 5, 5, 0, brand=True) == pytest.approx(5 * 0.682)                          # brand shots: Seedance 2.0 reference-to-video


def test_a_price_added_after_the_database_was_seeded_falls_back_to_the_shipped_default(env):
    row = db.fetch_one("select value from config where key = 'price_book'")["value"]
    del row["video"]["seedance_2_0_i2v"]
    from psycopg.types.json import Jsonb

    db.execute("update config set value = %s where key = 'price_book'", (Jsonb(row),))
    clear_config_cache()
    assert falmedia.estimate_cost("seedance_2_0_i2v", duration=4) == pytest.approx(2.728)
