import io
import json
import subprocess
import uuid
from types import SimpleNamespace

import numpy as np
import pytest
from helpers import at_gate, drive
from PIL import Image
from test_phase5 import ENV, brief, client, env, gate, jobs_of, png, runner, start, to_gate, voices  # noqa: F401 (fixtures)
from test_phase6 import build_story, job_for, recorder

from app import db
from app.config import clear_config_cache
from app.jobs.errors import PermanentError
from app.jobs.handlers import look_handlers as L
from app.jobs.handlers import story_handlers as H
from app.media import review
from app.models.look import EmotionGroup, EmotionGroupsOut, LineTag, LineTagsOut, VoiceProfile
from app.qa import face
from app.stages import look
from app.vendors import eleven, images, llm, local_voice, storage
from app.web.main import app


# ---------------------------------------------------------------- helpers

def look_ready(sid):
    """Story artefacts the look stage needs, built directly (no graph)."""
    build_story(sid)
    H.llm_wardrobe(job_for(sid, "llm_wardrobe"))


def spy_images(monkeypatch):
    calls, real = [], images.generate_image

    def spy(prompt, references=None, quality=None):
        calls.append(SimpleNamespace(prompt=prompt, references=references))
        return real(prompt, references, quality)

    monkeypatch.setattr(images, "generate_image", spy)
    return calls


def char(sid, code="CH01"):
    return db.fetch_one("select * from characters where series_id = %s and code = %s", (sid, code))


def run(handler, sid, job_kind, **req):
    return handler(job_for(sid, job_kind, None, **req))


def make_master(sid, code="CH01", use_photo=True, n=1):
    return run(L.image_master, sid, "image_master", character_id=str(char(sid, code)["id"]), **{"try": n}, use_photo=use_photo)


def pixels(data):
    return Image.open(io.BytesIO(data)).convert("RGB")


def white(img) -> int:
    """How many pixels are near-white (the caption text)."""
    return int((np.asarray(img).min(axis=2) > 200).sum())


# ---------------------------------------------------------------- review images

def test_review_images_carry_the_name_and_clean_images_carry_no_text():
    from app.vendors import fixtures

    clean = fixtures.fixture_bytes("image_1080x1920.png")
    a = review.caption_bar(clean, "Mira Haldar", "master portrait")
    b = review.caption_bar(clean, "Someone Else", "master portrait")
    ca, cl = pixels(a), pixels(clean)
    h = ca.size[1]
    assert ca.size == cl.size == (1080, 1920)
    top = (0, 0, 1080, h - review.BAR_HEIGHT)
    assert ca.crop(top).tobytes() == cl.crop(top).tobytes()               # the picture itself is untouched
    bar = ca.crop((0, h - review.BAR_HEIGHT, 1080, h))
    assert white(bar) > 300                                                           # white text pixels in the bar
    assert a != b and pixels(a).crop((0, h - 150, 1080, h)).tobytes() != pixels(b).crop((0, h - 150, 1080, h)).tobytes()   # the name is what differs
    assert review.caption_bar(clean, "মীরা হালদার", "master")                         # Bangla names do not crash


def test_stored_review_images_have_the_bar_and_clean_images_do_not(runner):
    c = client()
    sid = start(c)
    to_gate(runner, c, sid, 3)
    assets = db.fetch_all("select kind, r2_key_review, r2_key_clean from character_assets where series_id = %s and kind <> 'voice_sample'", (sid,))
    assert len(assets) >= 20
    fixture = pixels(open("tests/fixtures/image_1080x1920.png", "rb").read())
    for a in assets:
        assert a["r2_key_review"].endswith("_review.png") and a["r2_key_clean"].endswith("_clean.png")
        clean = pixels(look.fetch_bytes(a["r2_key_clean"]))
        rev = pixels(look.fetch_bytes(a["r2_key_review"]))
        assert clean.tobytes() == fixture.tobytes()                                        # exactly what the image model returned: no text
        bar = rev.crop((0, 1920 - review.BAR_HEIGHT, 1080, 1920))
        assert white(bar) > 300
    masters = db.fetch_all("select a.r2_key_review, c.name from character_assets a join characters c on c.id = a.character_id where a.series_id = %s and a.kind = 'master' order by c.code", (sid,))
    assert len(masters) == 2 and review.caption_bar(open("tests/fixtures/image_1080x1920.png", "rb").read(), masters[0]["name"], "master portrait") == look.fetch_bytes(masters[0]["r2_key_review"])


# ---------------------------------------------------------------- the whole stage in dry-run

def test_the_look_stage_writes_every_asset(runner):
    c = client()
    sid = start(c)
    to_gate(runner, c, sid, 3)
    kinds = {}
    for r in db.fetch_all("select c.code, a.kind, count(*) n from character_assets a left join characters c on c.id = a.character_id where a.series_id = %s group by 1, 2", (sid,)):
        kinds[(r["code"], r["kind"])] = r["n"]
    assert kinds[("CH01", "master")] == 1 and kinds[("CH01", "turnaround")] == 5 and kinds[("CH01", "expression")] == 3 and kinds[("CH01", "wardrobe")] == 2 and kinds[("CH01", "pose")] == 4
    assert kinds[("CH02", "master")] == 1 and kinds[("CH02", "expression")] == 3 and kinds[("CH02", "wardrobe")] == 1 and ("CH02", "turnaround") not in kinds
    assert kinds[(None, "lineup")] == 1 and kinds[("CH01", "voice_sample")] == 1 and kinds[("CH02", "voice_sample")] == 1
    assert db.fetch_one("select count(*) n from sets where series_id = %s and plate_r2_key is not null and ambience_r2_key is not null", (sid,))["n"] == 6
    assert len(jobs_of(sid, "llm_trait_sheet")) == 1 and len(jobs_of(sid, "llm_voice_profile")) == 2
    for r in db.fetch_all("select code, voice_profile, emotion_groups from characters where series_id = %s", (sid,)):
        assert r["voice_profile"]["accent"] == "Bangladeshi" and set(r["emotion_groups"].values()) == {"calm", "anger", "sadness"}
    ts = jobs_of(sid, "llm_trait_sheet")[0]["result"]["trait_sheet"]
    assert 30 <= len(ts.split()) <= 90
    voice = db.fetch_one("select tags, r2_key_clean from character_assets where series_id = %s and kind = 'voice_sample' limit 1", (sid,))
    assert voice["r2_key_clean"].endswith("_voice.mp3") and voice["tags"]["voice_id"].startswith("dry-voice-") and len(voice["tags"]["sample_text"]) > 20


# ---------------------------------------------------------------- inputs (what each image call is given)

def test_master_prompt_and_reference_order(env, monkeypatch):
    sid = start(client())
    look_ready(sid)
    out = run(L.llm_trait_sheet, sid, "llm_trait_sheet", ref_id=str(db.fetch_one("select id from brief_refs where series_id = %s and kind = 'character'", (sid,))["id"]))
    monkeypatch.setattr(look, "trait_sheet_for", lambda s, c: out["trait_sheet"])      # in production this comes from the job record
    seen = spy_images(monkeypatch)
    make_master(sid, use_photo=True, n=1)
    call = seen[0]
    assert call.prompt.count(look.MASTER_FIXED) == 1 and "a different person from any reference" in call.prompt
    assert char(sid)["visual_description"].strip() in call.prompt and "Look: warm amber light" in call.prompt and "Adult in their late twenties" in call.prompt   # the trait sheet is in
    photo = look.fetch_bytes(db.fetch_one("select r2_key from brief_refs where series_id = %s and kind = 'character'", (sid,))["r2_key"])
    assert call.references[0] == photo                                                                                                   # the photo comes first on the early tries
    db.execute("delete from character_assets where kind = 'master'")
    make_master(sid, use_photo=False, n=3)
    assert seen[1].references is None or photo not in seen[1].references                                                                 # the third try does not see it


def test_pack_prompts_use_the_master_as_the_reference(env, monkeypatch):
    sid = start(client())
    look_ready(sid)
    make_master(sid)
    c1 = char(sid)
    seen = spy_images(monkeypatch)
    master_bytes = look.fetch_bytes(db.fetch_one("select r2_key_clean from character_assets where kind = 'master'")["r2_key_clean"])
    for kind, tags in (("turnaround", {"angle": "three_quarter_left"}), ("expression", {"emotion_group": "anger"}), ("wardrobe", {"outfit": "W01", "view": "full_body_front"}), ("pose", {"pose": "arms_crossed"})):
        run(L.image_pack, sid, "image_pack", character_id=str(c1["id"]), kind=kind, tags=tags, **{"try": 0}, rev=0)
    prompts = [c.prompt for c in seen]
    assert all(c.references == [master_bytes] for c in seen) and all(p.startswith("The same person as the reference portrait.") for p in prompts)
    assert "three-quarter view turned to the left" in prompts[0] and "anger expression" in prompts[1]
    assert "Wearing: Dark jacket over a cream blouse." in prompts[2] and "full body, front view" in prompts[2] and "arms crossed pose" in prompts[3]
    rows = db.fetch_all("select kind, tags, face_vector is not null has_vec from character_assets where kind <> 'master' order by created_at")
    assert [r["kind"] for r in rows] == ["turnaround", "expression", "wardrobe", "pose"] and all(r["has_vec"] for r in rows)         # dry run: the master's face


def test_set_plate_and_lineup_inputs(env, monkeypatch):
    sid = start(client())
    look_ready(sid)
    make_master(sid)
    make_master(sid, "CH02")
    seen = spy_images(monkeypatch)
    sets = db.fetch_one("select id from sets where series_id = %s and code = 'SET01' and time_of_day = 'night'", (sid,))
    run(L.image_set_plate, sid, "image_set_plate", set_id=str(sets["id"]), rev=0)
    assert look.PLATE_FIXED in seen[0].prompt and "Time of day: night." in seen[0].prompt and "Look: warm amber light" in seen[0].prompt
    run(L.image_pack, sid, "image_pack", character_id=None, kind="lineup", tags={}, **{"try": 0}, rev=0)
    assert "side by side at true relative height" in seen[1].prompt and "CH01:" in seen[1].prompt and len(seen[1].references) == 2
    lineup = db.fetch_one("select character_id, face_vector from character_assets where kind = 'lineup'")
    assert lineup["character_id"] is None and lineup["face_vector"] is None


def test_the_trait_sheet_is_written_from_the_photo(env, monkeypatch):
    sid = start(client())
    look_ready(sid)
    seen = recorder(monkeypatch)
    ref = db.fetch_one("select id, r2_key from brief_refs where series_id = %s and kind = 'character'", (sid,))
    out = run(L.llm_trait_sheet, sid, "llm_trait_sheet", ref_id=str(ref["id"]))
    call = seen[-1]
    assert call.role == "writer" and "female lead" in call.messages[0]["content"] and "Never give a name" in call.system
    assert out["trait_sheet"] and out["ref_id"] == str(ref["id"])
    monkeypatch.setattr(L, "TRAIT_FIXTURE", "Too short.")
    with pytest.raises(PermanentError, match="trait sheet is 2 words"):
        run(L.llm_trait_sheet, sid, "llm_trait_sheet", ref_id=str(ref["id"]))


# ---------------------------------------------------------------- validators

def profile(**over):
    base = dict(voice_gender="female", voice_age="adult", pitch="low", pace="slow", accent="Bangladeshi", texture="warm", energy="calm", speech_habits="pauses",
                design_prompt="A calm adult woman with a warm low voice who speaks slowly, pausing before hard truths, with a quiet and restrained energy.")
    return VoiceProfile(**{**base, **over})


def test_voice_profile_rules():
    assert look.validate_voice_profile(profile(), "bn") == []
    assert any("Bangladeshi" in p for p in look.validate_voice_profile(profile(accent="neutral"), "bn"))
    assert look.validate_voice_profile(profile(accent="neutral"), "en") == []
    assert any("pitch must not be empty" in p for p in look.validate_voice_profile(profile(pitch=" "), "en"))
    assert any("design_prompt is 2 words" in p for p in look.validate_voice_profile(profile(design_prompt="A voice."), "en"))


def test_emotion_group_rules():
    emotions = ["angry", "furious", "sad", "calm"]
    good = EmotionGroupsOut(groups=[EmotionGroup(name="anger", emotions=["angry", "furious"]), EmotionGroup(name="sadness", emotions=["sad"]), EmotionGroup(name="calm", emotions=["calm"])])
    mapping, problems = look.emotion_map(good, emotions)
    assert problems == [] and mapping == {"angry": "anger", "furious": "anger", "sad": "sadness", "calm": "calm"}
    _, problems = look.emotion_map(EmotionGroupsOut(groups=[EmotionGroup(name="anger", emotions=["angry"])]), emotions)
    assert any("in no group: furious, sad, calm" in p for p in problems)
    _, problems = look.emotion_map(EmotionGroupsOut(groups=[EmotionGroup(name="a", emotions=["angry"]), EmotionGroup(name="b", emotions=["angry"])]), ["angry"])
    assert any("two groups" in p for p in problems)
    many = EmotionGroupsOut(groups=[EmotionGroup(name="g" * (i + 1), emotions=[f"e{i}"]) for i in range(9)])
    assert any("at most 8 groups, not 9" in p for p in look.emotion_map(many, [f"e{i}" for i in range(9)])[1])
    assert any("one lower-case word" in p for p in look.emotion_map(EmotionGroupsOut(groups=[EmotionGroup(name="Hot Anger", emotions=["x"])]), ["x"])[1])


def test_line_tag_rules():
    ok, problems = look.validate_line_tags(LineTagsOut(tags=[LineTag(idx=0, tag="Angry"), LineTag(idx=1, tag="quietly sad")]), [0, 1])
    assert problems == [] and ok == {0: "angry", 1: "quietly sad"}
    _, problems = look.validate_line_tags(LineTagsOut(tags=[LineTag(idx=0, tag="so very extremely angry"), LineTag(idx=7, tag="x")]), [0, 1])
    text = " | ".join(problems)
    assert "one to three words" in text and "was not asked for" in text and "no tag for lines: 0, 1" in text


def test_emotion_groups_and_line_tags_handlers(env, monkeypatch):
    sid = start(client())
    look_ready(sid)
    c1 = char(sid)
    run(L.llm_audio_tag, sid, "llm_audio_tag", purpose="emotion_groups", character_id=str(c1["id"]))
    assert char(sid)["emotion_groups"] == {"angry": "anger", "neutral": "calm", "sad": "sadness"}
    ep = db.fetch_one("select id from episodes where series_id = %s and number = 1", (sid,))
    seen = recorder(monkeypatch)
    db.execute("update dialogue_lines set audio_tag = 'done' where episode_id = %s and idx < 5", (ep["id"],))
    out = L.llm_audio_tag(job_for(sid, "llm_audio_tag", ep["id"], purpose="line_tags", episode=1, note="make it softer"))
    assert out["tagged"] == 10 and seen[-1].role == "copy"                                           # only the untagged lines; the cheap model
    assert "Reviewer note: make it softer" in seen[-1].messages[0]["content"] and "5:" in seen[-1].messages[0]["content"] and "\n4:" not in seen[-1].messages[0]["content"]
    assert db.fetch_one("select count(*) n from dialogue_lines where episode_id = %s and audio_tag is null", (ep["id"],))["n"] == 0
    assert L.llm_audio_tag(job_for(sid, "llm_audio_tag", ep["id"], purpose="line_tags", episode=1)) == {"tagged": 0}


def test_cost_rows_for_images_and_audio():
    r = L._result([{"model": "gpt-image-2.5-flare", "cost_usd": 0.1}, {"model": "local:stabilityai/sd-turbo", "cost_usd": 0.0}, {"model": "eleven_text_to_sound_v3", "cost_usd": 0.06}])
    assert r["actual_cost"] == pytest.approx(0.16)
    assert [(x["vendor"], x["unit_name"]) for x in r["cost_log"]] == [("openai", "image"), ("elevenlabs", "second")]        # free local work writes no cost row


# ---------------------------------------------------------------- voices

def test_voice_design_uses_the_characters_own_lines_and_saves_a_sample(env, monkeypatch):
    sid = start(client())
    look_ready(sid)
    c1 = char(sid)
    run(L.llm_voice_profile, sid, "llm_voice_profile", character_id=str(c1["id"]))
    asked = []
    real = eleven.design_voice
    monkeypatch.setattr(eleven, "design_voice", lambda d, t=None: (asked.append((d, t)), real(d, t))[1])
    out = run(L.voice_design, sid, "voice_design", character_id=str(c1["id"]), **{"try": 0}, rev=0)
    own = [r["text"] for r in db.fetch_all("select text from dialogue_lines where character_id = %s order by created_at, idx limit 12", (c1["id"],))]
    assert asked[0][0].startswith("A calm adult woman") and len(asked[0][1]) >= 100 and asked[0][1].startswith(own[0])      # previewed on the character's own lines
    asset = db.fetch_one("select * from character_assets where kind = 'voice_sample'")
    assert asset["tags"]["voice_id"] == out["voice_id"] and storage.local_file(asset["r2_key_clean"]).read_bytes()[:3] in (b"ID3", b"\xff\xfb", b"\xff\xf3")
    with pytest.raises(PermanentError, match="no voice profile"):
        run(L.voice_design, sid, "voice_design", character_id=str(char(sid, "CH02")["id"]), **{"try": 0}, rev=0)


def test_ambience_is_written_per_set(env):
    sid = start(client())
    look_ready(sid)
    run(L.sfx_ambience, sid, "sfx_ambience", set_code="SET01", rev=0)
    rows = db.fetch_all("select time_of_day, ambience_r2_key from sets where series_id = %s and code = 'SET01'", (sid,))
    assert len(rows) == 2 and {r["ambience_r2_key"] for r in rows} == {f"series/{sid}/sets/SET01_ambience.mp3"}


def test_local_voice_parameters_come_from_the_description():
    deep_man = local_voice.voice_params("A deep, calm older man with a measured pace")
    young_woman = local_voice.voice_params("A warm young woman, brisk and bright")
    assert deep_man[0] < -5 and deep_man[1] < 1 and young_woman[0] > 2 and young_woman[1] > 1
    assert local_voice.voice_params("a man")[0] < local_voice.voice_params("a woman")[0]
    spread = [local_voice.voice_params("a woman", i)[0] for i in range(3)]
    assert spread == sorted(spread) and len(set(spread)) == 3
    assert local_voice.voice_params("a very deep old man " * 5)[0] >= -8                       # clamped
    assert local_voice.parse_voice_id(local_voice.make_voice_id(-3.5, 0.93)) == (-3.5, 0.93)
    for bad in ("dry-voice-1", "local:x:y", ""):
        with pytest.raises(PermanentError):
            local_voice.parse_voice_id(bad)
    cleaned = local_voice.strip_tags("[angry] Get out [pause] now")
    assert "[" not in cleaned and cleaned.split() == ["Get", "out", "now"]


def test_the_voice_switch_routes_to_the_local_backend(env, monkeypatch):
    monkeypatch.setenv("VOICE_BACKEND", "local")
    monkeypatch.setattr(local_voice, "speech", lambda text, vid, lang, seed: b"MP3" + text.encode())
    monkeypatch.setattr(local_voice, "design", lambda desc, sample=None, count=3: [{"generated_voice_id": "local:1.0:1.00", "audio": b"x", "duration_secs": None}])
    out, usage = eleven.speech("[angry] Hello there", "local:1.0:1.00", "en", 1)
    assert out["audio"].startswith(b"MP3") and out["alignment"] is None and out["words"] == [] and usage["cost_usd"] == 0 and usage["model"].startswith("local:")
    previews, usage = eleven.design_voice("a woman")
    assert previews[0]["generated_voice_id"] == "local:1.0:1.00" and eleven.save_voice("local:1.0:1.00", "n", "d") == "local:1.0:1.00"
    monkeypatch.delenv("VOICE_BACKEND")
    assert eleven.speech("Hello", "x", "en", 1)[1]["cost_usd"] > 0                              # back to the fixture


def duration(data: bytes) -> float:
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".mp3") as f:
        f.write(data)
        f.flush()
        return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", f.name], capture_output=True, text=True).stdout)


def test_procedural_ambience_and_music_are_real_audio(env, monkeypatch):
    monkeypatch.setenv("VOICE_BACKEND", "local")
    amb, usage = eleven.sound_effect("a quiet rainy kitchen", 6, loop=True)
    assert 5.8 < duration(amb) < 6.3 and usage["cost_usd"] == 0
    assert amb != eleven.sound_effect("a loud market", 6)[0]
    music, _ = eleven.music("tense underscore", 5000)
    assert 4.8 < duration(music) < 5.4
    with pytest.raises(PermanentError, match="English-only"):
        local_voice.speech("hello", "local:0.0:1.00", "bn", 1)
    with pytest.raises(PermanentError, match="nothing to say"):
        local_voice.speech("[angry]", "local:0.0:1.00", "en", 1)


# ---------------------------------------------------------------- media route and the Gate 3 page

def test_media_route(runner, monkeypatch):
    c = client()
    sid = start(c)
    drive(runner, sid, at_gate(2))
    assert gate(c, sid, 2, decision="approve").status_code == 303
    drive(runner, sid, at_gate(3))
    key = db.fetch_one("select r2_key_review k from character_assets where kind = 'master' limit 1")["k"]
    path = key[len(f"series/{sid}/"):]
    from fastapi.testclient import TestClient

    assert TestClient(app, base_url="https://testserver").get(f"/media/{sid}/{path}").status_code == 401
    r = c.get(f"/media/{sid}/{path}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content == look.fetch_bytes(key)
    assert client("other@x.io").get(f"/media/{sid}/{path}").status_code == 200                  # any active member may view
    assert c.get(f"/media/{sid}/characters/CH01/nope.png").status_code == 404
    assert c.get(f"/media/{sid}/characters/%2e%2e/%2e%2e/brief/x").status_code == 404
    assert c.get(f"/media/{uuid.uuid4()}/{path}").status_code == 404
    monkeypatch.setattr(storage, "is_local", lambda: False)
    monkeypatch.setattr(storage, "presign_get", lambda k, s=3600: f"https://r2.example/{k}?expires={s}")
    r = c.get(f"/media/{sid}/{path}", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == f"https://r2.example/{key}?expires=3600"


def test_the_look_review_page_and_approving_through_it(runner):
    c = client()
    sid = start(c)
    drive(runner, sid, at_gate(2))
    gate(c, sid, 2, decision="approve")
    drive(runner, sid, at_gate(3))
    page = c.get(f"/productions/{sid}/review/look")
    assert page.status_code == 200
    for needle in ("CH01 · Lead 1", "Your reference photo (female lead)", "Master portrait", "Similarity to your reference photo", "turnaround", "wardrobe", "audio controls", "Cast line-up",
                   "SET01 Place 1", "Approve the look and voices", "swap this voice", 'name="redo"'):
        assert needle in page.text, needle
    assert "Review the look" in c.get(f"/productions/{sid}").text
    assert "Approve the look" not in client("other@x.io").get(f"/productions/{sid}/review/look").text
    voices_json = page.text.split('name="voices" value="')[1].split('"')[0].replace("&#34;", '"').replace("&quot;", '"')
    chosen = json.loads(voices_json)
    assert set(chosen) == {"CH01", "CH02"} and all(v.startswith("dry-voice-") for v in chosen.values())          # one voice per speaking character, pre-filled
    assert c.post(f"/productions/{sid}/gate/3", data={"decision": "approve", "voices": voices_json}).status_code == 303
    drive(runner, sid, at_gate(4))
    assert {r["code"]: r["voice_id"] for r in db.fetch_all("select code, voice_id from characters where series_id = %s", (sid,))} == chosen
    assert "Approve the look" not in c.get(f"/productions/{sid}/review/look").text                                  # the gate is closed


def test_ticked_assets_are_redone_and_nothing_else(runner):
    c = client()
    sid = start(c)
    drive(runner, sid, at_gate(2))
    gate(c, sid, 2, decision="approve")
    drive(runner, sid, at_gate(3))
    voice_asset = db.fetch_one("select id from character_assets where series_id = %s and kind = 'voice_sample' order by created_at limit 1", (sid,))["id"]
    pose = db.fetch_one("select id from character_assets where series_id = %s and kind = 'pose' limit 1", (sid,))["id"]
    packs, voices_before = len(jobs_of(sid, "image_pack")), len(jobs_of(sid, "voice_design"))
    r = c.post(f"/productions/{sid}/gate/3", data={"decision": "reject", "notes": "swap that voice", "redo": [str(voice_asset), str(pose)]})
    assert r.status_code == 303
    drive(runner, sid, at_gate(3))
    assert len(jobs_of(sid, "image_pack")) == packs + 1 and len(jobs_of(sid, "voice_design")) == voices_before + 1 and len(jobs_of(sid, "image_master")) == 2
    assert db.fetch_one("select count(*) n from character_assets where id in (%s, %s)", (voice_asset, pose))["n"] == 0


def test_a_master_that_stayed_too_similar_is_flagged_on_the_page(runner, monkeypatch):
    real = face.stub_vector
    monkeypatch.setattr(face, "stub_vector", lambda seed: real("CH01:1") if seed.startswith("CH01") else real(seed))
    monkeypatch.setattr(face, "face_vector", lambda data: real("CH01:1"))
    c = client()
    sid = start(c)
    drive(runner, sid, at_gate(2))
    gate(c, sid, 2, decision="approve")
    drive(runner, sid, at_gate(3))
    page = c.get(f"/productions/{sid}/review/look").text
    assert "too similar even after regenerating" in page and "1.00" in page


def test_images_sent_to_the_model_are_labelled_with_their_real_type():
    import base64

    out = io.BytesIO()
    Image.new("RGB", (8, 8)).save(out, "JPEG")
    for fmt, expected in (("PNG", "image/png"), ("JPEG", "image/jpeg"), ("GIF", "image/gif"), ("WEBP", "image/webp")):
        buf = io.BytesIO()
        Image.new("RGB", (8, 8)).save(buf, fmt)
        block = llm._image_block(buf.getvalue())
        assert block["image_url"]["url"].startswith(f"data:{expected};base64,"), fmt
        assert base64.b64decode(block["image_url"]["url"].split(",", 1)[1]) == buf.getvalue()
    assert llm._image_block("https://example.com/x.png") == {"type": "image_url", "image_url": {"url": "https://example.com/x.png"}}
