"""Phase 8: build one episode (guide 9.6 and 10). Real media code runs on the fixture clips."""
import json
import subprocess
from pathlib import Path

import pytest
from test_phase5 import brief, client, env, runner, start, to_gate  # noqa: F401 (fixtures)
from test_phase6 import build_story, job_for, recorder
from test_phase7 import char, look_ready, make_master, run, spy_images
from test_phase7 import L

from app import db
from app.jobs.errors import PermanentError
from app.jobs.handlers import build_handlers as B
from app.media import compile as media_compile, durations, ffmpeg, subtitles
from app.models.shots import CameraSetupDef, ShotCharacter, ShotList, ShotRequest, ShotSpec
from app.stages import shots as S
from app.vendors import fixtures, images, storage

TIMING = {"pad_head": 0.25, "pad_tail": 0.35, "line_gap": 0.30}
FIXTURE_CLIP = fixtures.fixture_path("clip_4s_1080x1920.mp4")


def episode(sid, n=1):
    return db.fetch_one("select * from episodes where series_id = %s and number = %s", (sid, n))


def planned_story(runner_unused=None):
    sid = start(client())
    look_ready(sid)
    return sid, episode(sid)


# ---------------------------------------------------------------- shot list checks

def test_the_fixture_shot_list_passes_every_check_and_each_rule_can_fail(env):
    sid, ep = planned_story()
    good = S.fixture_shot_list(sid, ep)
    known = {"Static medium"}
    assert S.validate_shot_list(sid, ep, good.shots, known) == []
    assert 8 <= len(good.shots) <= 12 and good.shots[0].is_hook and good.shots[-1].is_cliffhanger

    def with_(index=None, **change):
        shots = [s.model_copy() for s in good.shots]
        if index is not None:
            shots[index] = shots[index].model_copy(update=change)
        return shots

    speaking = next(i for i, s in enumerate(good.shots) if s.line_indexes)
    silent = next(i for i, s in enumerate(good.shots) if not s.line_indexes)
    cases = {
        "needs 8 to 12 shots": good.shots[:4],
        "first shot must be the hook": with_(0, is_hook=False),
        "last shot must be the cliffhanger": with_(len(good.shots) - 1, is_cliffhanger=False),
        "every dialogue line must be used once": with_(speaking, line_indexes=good.shots[speaking].line_indexes[:-1]),
        "does not exist": with_(silent, set_code="SET99"),
        "has no outfit": with_(silent, characters=[ShotCharacter(code="CH01", outfit_code="W99", position="x", mouth_visible_speaking=False)]),
        "exactly one character with mouth_visible_speaking": with_(speaking, characters=[ShotCharacter(code="CH01", outfit_code="W01", position="x", mouth_visible_speaking=False)]),
        "est_seconds must be between 2 and 10": with_(silent, est_seconds=30),
        "is not in the list": with_(silent, camera_setup_name="Nonexistent", new_setup=None),
        "must equal new_setup.name": with_(silent, camera_setup_name="Other"),
    }
    for needle, shots in cases.items():
        problems = S.validate_shot_list(sid, ep, shots, known)
        assert any(needle in p for p in problems), (needle, problems)
    owner = good.shots[speaking].characters[0].code                       # lines that belong to someone else
    other = "CH02" if owner == "CH01" else "CH01"
    wrong = with_(speaking, characters=[ShotCharacter(code=other, outfit_code="W01", position="x", mouth_visible_speaking=True)])
    assert any("must belong to" in p for p in S.validate_shot_list(sid, ep, wrong, known))


def test_a_shot_must_wear_the_outfit_the_wardrobe_plan_gives(env):
    sid, ep = planned_story()
    db.execute("update wardrobe_items set scenes = '[{\"episode\": 1, \"scene\": 0}]'::jsonb where code = 'W01'")
    db.execute("insert into wardrobe_items (series_id, character_id, code, description, scenes) select series_id, id, 'W02', 'A red coat.', '[]'::jsonb from characters where series_id = %s and code = 'CH01'", (sid,))
    shots = [s.model_copy() for s in S.fixture_shot_list(sid, ep).shots]
    first = next(i for i, s in enumerate(shots) if s.scene_index == 0 and any(c.code == "CH01" for c in s.characters))
    shots[first] = shots[first].model_copy(update={"characters": [ShotCharacter(code="CH01", outfit_code="W02", position="x", mouth_visible_speaking=bool(shots[first].line_indexes))]})
    assert any("wardrobe plan has CH01 in W01" in p for p in S.validate_shot_list(sid, ep, shots, {"Static medium"}))


def test_the_shot_list_handler_stores_shots_and_new_setups_once(env):
    sid, ep = planned_story()
    for _ in range(2):                                                                    # a redo must not duplicate the setup
        out = B.llm_shot_list(job_for(sid, "llm_shot_list", ep["id"], episode=1))
        assert out["shots"] >= 8
    rows = db.fetch_all("select * from shots where episode_id = %s order by idx", (ep["id"],))
    assert [r["idx"] for r in rows] == list(range(len(rows))) and episode(sid)["status"] == "shots_planned"
    assert db.fetch_one("select count(*) n from camera_setups where name = 'Static medium'")["n"] == 1
    setup = db.fetch_one("select * from camera_setups where name = 'Static medium'")
    assert setup["origin_series_id"] == sid and setup["embedding"] is not None
    assert all(r["camera_setup_id"] == setup["id"] for r in rows)
    speaking = [r for r in rows if r["line_ids"]]
    assert all(r["tier"] in ("B1", "B2", "C") and r["speaker_character_id"] for r in speaking)
    assert all(r["tier"] == "A" and r["speaker_character_id"] is None for r in rows if not r["line_ids"])
    assert all(r["spec"]["emotion_group"] for r in rows) and all("est_seconds" in r["spec"] for r in rows)
    used = [i for r in rows for i in r["spec"]["line_indexes"]]
    assert used == list(range(len(used)))


def test_a_bad_shot_list_fails_the_job_with_every_problem(env, monkeypatch):
    sid, ep = planned_story()
    good = S.fixture_shot_list
    monkeypatch.setattr(S, "fixture_shot_list", lambda s, e: ShotList(episode_number=1, shots=good(s, e).shots[:3]))
    with pytest.raises(PermanentError, match="needs 8 to 12 shots"):
        B.llm_shot_list(job_for(sid, "llm_shot_list", ep["id"], episode=1))
    assert db.fetch_one("select count(*) n from shots where episode_id = %s", (ep["id"],))["n"] == 0        # nothing stored


def test_the_director_sees_the_script_setups_notes_and_standing_notes(env, monkeypatch):
    sid, ep = planned_story()
    seen = recorder(monkeypatch)
    B.llm_shot_list(job_for(sid, "llm_shot_list", ep["id"], episode=1, standing="Prefer close-ups."))
    call = seen[-1]
    assert call.role == "director" and call.schema == "ShotList" and call.cache
    body = call.messages[0]["content"]
    assert "Prefer close-ups." in body and "Scene 0 in SET01" in body and "[0] CH01" in body and "Outfits:" in body
    assert "Lead Director" in call.system and "mouth_visible_speaking" in call.system


# ---------------------------------------------------------------- durations

def test_a_short_line_on_b2_renders_at_four_seconds_and_is_trimmed(tmp_path):
    fit = durations.fit_episode([{"idx": 0, "tier": "B2", "line_durations": [1.2], "est_seconds": None}], TIMING)
    assert fit["planned"][0] == pytest.approx(1.8) and fit["render"][0] == 4                  # the model's minimum is 4 s
    out = tmp_path / "v.mp4"
    media_compile.shot_video(FIXTURE_CLIP, fit["planned"][0], out)
    info = ffmpeg.probe(out)
    assert info["duration"] == pytest.approx(1.8, abs=0.06) and (info["width"], info["height"], info["fps"]) == (1080, 1920, 24.0)


def test_a_clip_shorter_than_planned_holds_its_last_frame(tmp_path):
    out = tmp_path / "v.mp4"
    media_compile.shot_video(FIXTURE_CLIP, 6.0, out)                                          # the fixture clip is 4 s
    assert ffmpeg.probe(out)["duration"] == pytest.approx(6.0, abs=0.06)


def test_a_b1_shot_is_at_least_two_seconds_because_of_the_audio_limit():
    fit = durations.fit_episode([{"idx": 0, "tier": "B1", "line_durations": [1.0], "est_seconds": None}], TIMING)
    assert fit["planned"][0] == 2.0 and fit["render"][0] == 2


# ---------------------------------------------------------------- shot audio

def lines_of(tmp_path, n=2):
    files = []
    for i in range(n):
        f = tmp_path / f"l{i}.mp3"
        f.write_bytes(fixtures.fixture_bytes("speech_2s.mp3"))
        files.append(f)
    return files, [ffmpeg.duration(f) for f in files]


def test_shot_audio_is_padded_to_exactly_the_video_length(tmp_path):
    files, secs = lines_of(tmp_path)
    for total in (secs[0] + secs[1] + 0.25 + 0.3 + 0.35, 9.0):                                # exact fit, and padded up to a longer clip
        out = tmp_path / "a.mp3"
        info = ffmpeg.build_shot_audio(files, secs, TIMING, total, out)
        assert info["seconds"] == pytest.approx(total, abs=0.07)
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,sample_rate,channels,bit_rate", "-of", "json", str(out)], capture_output=True, text=True).stdout
        stream = json.loads(probe)["streams"][0]
        assert (stream["codec_name"], stream["sample_rate"], stream["channels"]) == ("mp3", "44100", 1) and int(stream["bit_rate"]) == 128000


def test_shot_audio_refuses_lengths_outside_the_lipsync_limits(tmp_path):
    files, secs = lines_of(tmp_path, 1)
    for total in (1.5, 61):
        with pytest.raises(PermanentError, match="must be 2 to 60 s"):
            ffmpeg.build_shot_audio(files, secs, TIMING, total, tmp_path / "a.mp3")


# ---------------------------------------------------------------- captions

def test_captions_use_chunks_of_four_bangla_words_or_five_english_words():
    text = " ".join(f"w{i}" for i in range(10))
    assert [c[2] for c in subtitles.line_chunks(text, "bn", 0, 4.0, None)] == ["w0 w1 w2 w3", "w4 w5 w6 w7", "w8 w9"]
    assert [c[2] for c in subtitles.line_chunks(text, "en", 0, 4.0, None)] == ["w0 w1 w2 w3 w4", "w5 w6 w7 w8 w9"]


def test_caption_times_come_from_the_word_timings_or_spread_evenly():
    words = [{"w": f"x{i}", "start": i * 0.5, "end": i * 0.5 + 0.4} for i in range(8)]
    a, b = subtitles.line_chunks(" ".join(f"w{i}" for i in range(8)), "bn", 10.0, 4.0, words)
    assert a[:2] == (10.0, pytest.approx(12.0)) and b[0] == pytest.approx(12.0)      # 1.9 + 0.15 hold is clamped to the next chunk start
    even = subtitles.line_chunks(" ".join(f"w{i}" for i in range(8)), "bn", 10.0, 4.0, None)
    assert [(round(s, 2), round(e, 2)) for s, e, _ in even] == [(10.0, 12.0), (12.0, 14.0)]
    short = subtitles.line_chunks("a b c d e f", "bn", 0, 3.0, [{"w": "ab", "start": 0, "end": 1}, {"w": "cd", "start": 1, "end": 2}, {"w": "ef", "start": 2, "end": 3}])   # respelling has fewer words
    assert short[0][0] == 0 and short[-1][1] == pytest.approx(3.15)


def test_the_caption_file_has_the_guides_header_and_style():
    events = subtitles.episode_events("bn", [{"start": 5.0, "lines": [{"text": "আমি জানি", "duration": 2.0, "words": None}, {"text": "তুমি কী করেছ", "duration": 2.0, "words": None}]}], TIMING)
    ass = subtitles.build_ass("bn", events)
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass and "Style: Default,Noto Sans Bengali,64," in ass and ",4,0,2,60,60,260,1" in ass
    assert "Dialogue: 0,0:00:05.25,0:00:07.25,Default,,0,0,0,,আমি জানি" in ass                     # 5.0 + pad_head
    assert "0:00:07.55" in ass                                                                    # + line 1 (2.0) + line_gap (0.3)
    assert "Style: Default,Noto Sans," in subtitles.build_ass("en", [])


def grab(video: Path, t: float, out: Path):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1", str(out)], check=True)
    from PIL import Image

    return Image.open(out).convert("RGB")


def white_in_caption_zone(img) -> int:
    import numpy as np

    return int((np.asarray(img.crop((0, 1450, 1080, 1700))).min(axis=2) > 235).sum())


def test_a_bangla_caption_renders_with_the_bengali_font(tmp_path):
    base = tmp_path / "black.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=1080x1920:r=24:d=3", "-pix_fmt", "yuv420p", str(base)], check=True)
    ass = tmp_path / "c.ass"
    text = "আমি জানি তুমি কী করেছ ক্ষমা"
    ass.write_text(subtitles.build_ass("bn", [(0.5, 2.5, text)]), encoding="utf-8")
    silent = tmp_path / "s.wav"
    ffmpeg.run(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "3", str(silent)])
    final = tmp_path / "f.mp4"
    media_compile.encode_final(base, silent, ass, final)
    on, off = grab(final, 1.5, tmp_path / "on.png"), grab(final, 0.1, tmp_path / "off.png")
    assert white_in_caption_zone(off) == 0 and white_in_caption_zone(on) > 1500                      # glyphs drawn, in the lower area, not blank boxes
    ys = [y for y in range(1450, 1700) if any(min(on.getpixel((x, y))) > 235 for x in range(0, 1080, 3))]
    assert ys and min(ys) > 1500 and max(ys) < 1700                                                   # bottom-centre with the 260 px margin


# ---------------------------------------------------------------- keyframe references and prompt

def test_keyframe_references_use_whatever_sheets_exist_and_fall_back_to_the_master(env, monkeypatch):
    sid, ep = planned_story()
    make_master(sid)
    spec = {"set_code": "SET01", "time_of_day": "day", "emotion_group": "anger", "action": "She turns.",
            "characters": [{"code": "CH01", "outfit_code": "W01", "position": "frame centre", "mouth_visible_speaking": True}]}
    refs, labels, master = B.keyframe_references(sid, spec)
    assert len(refs) == 1 and master is not None and "reference image 1" in labels[0]                  # only the master portrait exists (sheets skipped)
    monkeypatch.setenv("SKIP_PACK_KINDS", "none")
    spy_images(monkeypatch)
    cid = str(char(sid)["id"])
    run(L.image_pack, sid, "image_pack", character_id=cid, kind="wardrobe", tags={"outfit": "W01", "view": "full_body_front"}, **{"try": 0}, rev=0)
    run(L.image_pack, sid, "image_pack", character_id=cid, kind="expression", tags={"emotion_group": "anger"}, **{"try": 0}, rev=0)
    run(L.image_set_plate, sid, "image_set_plate", set_id=str(db.fetch_one("select id from sets where series_id = %s and code = 'SET01' and time_of_day = 'day'", (sid,))["id"]), rev=0)
    refs, labels, _ = B.keyframe_references(sid, spec)
    assert len(refs) == 4 and "wears the outfit" in labels[1] and "expression follows" in labels[2] and "the place is reference image 4" in labels[3]
    spec["characters"][0]["mouth_visible_speaking"] = False
    spec["characters"].append({"code": "CH02", "outfit_code": "W01", "position": "left", "mouth_visible_speaking": False})
    make_master(sid, "CH02")
    refs, _, _ = B.keyframe_references(sid, spec)
    assert len(refs) == 4 and len(refs) <= images.MAX_REFERENCES      # CH01 master+outfit, CH02 master, the place (no expression: nobody speaks)


def test_the_keyframe_prompt_has_the_camera_setup_and_no_names_or_text(env):
    sid, ep = planned_story()
    B.llm_shot_list(job_for(sid, "llm_shot_list", ep["id"], episode=1))
    shot = db.fetch_one("select s.*, e.number episode_number from shots s join episodes e on e.id = s.episode_id where s.episode_id = %s and s.idx = 1", (ep["id"],))
    prompt = B.keyframe_prompt(sid, shot, ["person 1: the person in reference image 1"], 0)
    assert "Camera: medium shot, eye level angle" in prompt and "Place 1" in prompt and "wearing Dark jacket over a cream blouse." in prompt
    assert "No text, no captions, no logos, no watermark." in prompt and "Lead 1" not in prompt and "CH01" not in prompt
    assert "Variation 1." in B.keyframe_prompt(sid, shot, [], 1)


def test_a_keyframe_job_stores_the_image_checks_the_face_and_fills_the_cache(env):
    sid, ep = planned_story()
    make_master(sid)
    make_master(sid, "CH02")
    B.llm_shot_list(job_for(sid, "llm_shot_list", ep["id"], episode=1))
    shot = db.fetch_one("select id from shots where episode_id = %s and tier = 'A' order by idx limit 1", (ep["id"],))
    out = B.image_keyframe({**job_for(sid, "image_keyframe", ep["id"]), "shot_id": shot["id"]})
    row = db.fetch_one("select keyframe_r2_key, status from shots where id = %s", (shot["id"],))
    assert row["status"] == "keyframe_ready" and row["keyframe_r2_key"].endswith("keyframe_a0.png") and out["references"] >= 1
    assert images.TARGET == (1080, 1920)
    same = db.fetch_one("select count(*) n from shots where episode_id = %s and keyframe_r2_key = %s", (ep["id"], row["keyframe_r2_key"]))["n"]
    assert same >= 1                                                                                  # shots with the same cache key took it too


# ---------------------------------------------------------------- shot request rules

def shot_of(sid, tier):
    sql = "select s.*, e.number episode_number from shots s join episodes e on e.id = s.episode_id where s.series_id = %s and s.tier = %s order by s.idx limit 1"
    return db.fetch_one(sql, (sid, tier))


def test_shot_request_prompt_rules(env):
    sid, ep = planned_story()
    B.llm_shot_list(job_for(sid, "llm_shot_list", ep["id"], episode=1))
    a, b2 = shot_of(sid, "A"), shot_of(sid, "B2")
    line = db.fetch_one("select text from dialogue_lines where id = %s", (b2["line_ids"][0],))["text"]
    ok_a = ShotRequest(prompt="She turns slowly toward the window. Sounds: rain. No music. No speech.", camera_fixed=False)
    assert S.validate_request(sid, a, ok_a) == []
    assert any("must end with" in p for p in S.validate_request(sid, a, ShotRequest(prompt="She turns.", camera_fixed=False)))
    assert any("names the character" in p for p in S.validate_request(sid, a, ShotRequest(prompt="Lead 1 turns. No music. No speech.", camera_fixed=False)))
    assert any("on-screen text" in p for p in S.validate_request(sid, a, ShotRequest(prompt="A caption appears. No music. No speech.", camera_fixed=False)))
    assert any("1500" in p for p in S.validate_request(sid, a, ShotRequest(prompt="x" * 1501 + " No music. No speech.", camera_fixed=False)))
    speaking = S.fixture_request(b2, "kling_v3_pro")
    assert S.validate_request(sid, b2, speaking) == [] and line in speaking.prompt
    bad = ShotRequest(prompt="She speaks. No other character speaks.", camera_fixed=False)
    assert any("must contain" in p for p in S.validate_request(sid, b2, bad)) and any("line text" in p for p in S.validate_request(sid, b2, bad))
    b1 = shot_of(sid, "B1")
    assert S.validate_request(sid, b1, S.fixture_request(b1, "kling_avatar_v2_pro")) == []
    assert any("one sentence" in p for p in S.validate_request(sid, b1, ShotRequest(prompt="She breathes in. Then she speaks.", camera_fixed=True)))


def test_a_line_that_says_a_name_is_allowed_inside_the_spoken_text(env):
    sid, ep = planned_story()
    B.llm_shot_list(job_for(sid, "llm_shot_list", ep["id"], episode=1))
    b2 = shot_of(sid, "B2")
    db.execute("update dialogue_lines set text = text || ' Lead 1' where id = %s", (b2["line_ids"][0],))
    assert S.validate_request(sid, b2, S.fixture_request(b2, "kling_v3_pro")) == []


def test_the_compiler_gets_the_camera_text_for_its_model_and_the_nearest_rules(env, monkeypatch):
    sid, ep = planned_story()
    B.llm_shot_list(job_for(sid, "llm_shot_list", ep["id"], episode=1))
    db.execute("update camera_setups set prompt_by_model = %s::jsonb", (json.dumps({"kling_v3_pro": "slow dolly in, shallow focus"}),))
    for i, model in enumerate(("kling_v3_pro", "kling_v3_pro", "seedance_2_0_i2v")):
        (vec,), _ = images.embed([f"rule {i}"])
        db.execute("insert into learned_rules (model_key, rule_text, category, embedding) values (%s, %s, 'artefact', %s::vector)", (model, f"Rule number {i}.", db.to_vector(vec)))
    seen = recorder(monkeypatch)
    a = shot_of(sid, "A")
    out = B.llm_shot_request({**job_for(sid, "llm_shot_request", ep["id"], model_key="kling_v3_pro"), "shot_id": a["id"]})
    body = seen[-1].messages[0]["content"]
    assert "slow dolly in, shallow focus" in body and "Rule number 0." in body and "Rule number 1." in body and "Rule number 2." not in body
    assert seen[-1].role == "compiler" and out["prompt"].endswith("No music. No speech.") and len(out["rule_ids"]) == 2


# ---------------------------------------------------------------- the whole episode, with the real compile

@pytest.fixture()
def pilot(runner, monkeypatch):
    c = client()
    sid = start(c)
    to_gate(runner, c, sid, 4)
    monkeypatch.delenv("FAST_COMPILE")
    monkeypatch.setenv("FFMPEG_PRESET", "ultrafast")
    return sid


def test_lipsync_audio_is_exactly_as_long_as_the_clip(pilot):
    shot = db.fetch_one("select s.*, e.number episode_number from shots s join episodes e on e.id = s.episode_id where s.series_id = %s and s.tier = 'B2' limit 1", (pilot,))
    audio_key = B.shot_audio_key(pilot, shot)
    raw = storage.local_file(shot["raw_clip_r2_key"])
    assert ffmpeg.duration(storage.local_file(audio_key)) == pytest.approx(ffmpeg.duration(raw), abs=0.07)


def test_a_compiled_episode_is_1080x1920_at_24_fps_and_75_to_90_seconds(pilot):
    ep = episode(pilot)
    out = B.ffmpeg_compile(job_for(pilot, "ffmpeg_compile", ep["id"], episode=1, cut=9))
    ep = episode(pilot)
    final = storage.local_file(ep["final_r2_key"])
    info = ffmpeg.probe(final)
    assert (info["width"], info["height"], info["fps"]) == (1080, 1920, 24.0) and info["has_audio"]
    assert 75 <= info["duration"] <= 90 and info["duration"] == pytest.approx(float(ep["runtime_seconds"]), abs=0.25)
    assert ep["cut_version"] == 9 and ep["status"] == "compiled" and ep["final_r2_key"].endswith("final_v9.mp4") and ep["subtitle_r2_key"].endswith("final_v9.ass")
    # edit_entries: one per shot, contiguous from 0 to the runtime, no gaps and no overlaps
    rows = db.fetch_all("select e.start_seconds s, e.end_seconds en, sh.idx, sh.planned_duration p from edit_entries e join shots sh on sh.id = e.shot_id where e.episode_id = %s and e.cut_version = 9 order by sh.idx", (ep["id"],))
    assert len(rows) == db.fetch_one("select count(*) n from shots where episode_id = %s", (ep["id"],))["n"]
    assert float(rows[0]["s"]) == 0 and all(float(a["en"]) == pytest.approx(float(b["s"])) for a, b in zip(rows, rows[1:]))
    assert all(float(r["en"]) - float(r["s"]) == pytest.approx(float(r["p"])) for r in rows) and float(rows[-1]["en"]) == pytest.approx(float(ep["runtime_seconds"]))
    assert out["runtime"] == pytest.approx(float(ep["runtime_seconds"]))
    # audio: loudness near the -16 LUFS target (music and captions in place)
    vol = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(final), "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True).stderr
    assert "mean_volume" in vol and float(vol.split("max_volume:")[1].split("dB")[0]) <= 0.0


def test_the_burned_in_caption_is_in_the_final_frame_and_is_bangla(pilot, tmp_path):
    ep = episode(pilot)
    B.ffmpeg_compile(job_for(pilot, "ffmpeg_compile", ep["id"], episode=1, cut=9))
    ep = episode(pilot)
    ass = storage.local_file(ep["subtitle_r2_key"]).read_text(encoding="utf-8")
    first = next(l for l in ass.splitlines() if l.startswith("Dialogue:"))
    start = first.split(",")[1]
    h, m, s = start.split(":")
    t = int(h) * 3600 + int(m) * 60 + float(s) + 0.3
    assert any("ঀ" <= ch <= "৿" for ch in first) and "Noto Sans Bengali" in ass                  # the series language is Bangla
    final = storage.local_file(ep["final_r2_key"])
    assert white_in_caption_zone(grab(final, t, tmp_path / "on.png")) > white_in_caption_zone(grab(final, 0.05, tmp_path / "off.png")) + 300


def test_a_set_insert_is_a_zoomed_loop_of_the_plate(pilot):
    shot = db.fetch_one("select s.*, e.number episode_number from shots s join episodes e on e.id = s.episode_id where s.series_id = %s and s.idx = 0 limit 1", (pilot,))
    db.execute("update shots set planned_duration = 2.5, tier = 'INSERT', shot_type = 'static_insert' where id = %s", (shot["id"],))
    B.ffmpeg_insert({**job_for(pilot, "ffmpeg_insert", shot["episode_id"]), "shot_id": shot["id"]})
    key = db.fetch_one("select final_clip_r2_key from shots where id = %s", (shot["id"],))["final_clip_r2_key"]
    info = ffmpeg.probe(storage.local_file(key))
    assert (info["width"], info["height"], info["fps"]) == (1080, 1920, 24.0) and info["duration"] == pytest.approx(2.5, abs=0.1) and not info["has_audio"]
