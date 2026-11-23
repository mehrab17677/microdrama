"""Ad placement (docs/AD_PLACEMENT.md): the real product appears INSIDE generated scenes; its pictures are references, the checks compare the result with them."""
import json
import uuid

import pytest
from helpers import at_gate, drive, screen
from test_phase5 import DESCRIPTION, ad_brief, brief, client, env, gate, jobs_of, post_json, runner, start, to_gate  # noqa: F401 (fixtures)
from test_phase6 import job_for, recorder

from app import db, decisions, placement
from app.graphs import work
from app.jobs.errors import PermanentError
from app.jobs.handlers import HANDLERS, build_handlers as B, brand_handlers as BH, qa_handlers as Q
from app.media import costs, requests as media_requests, routing
from app.qa import brand as brand_qa, repair
from app.stages import brand, brand_qa as ep_brand, shots as S
from app.vendors import falmedia, images, llm, storage


def ad_production(c, kinds=("product_in_use", "product_shown"), **over):
    data, files = ad_brief(kinds=kinds, **over)
    r = post_json(c, data, files)
    assert r.status_code == 303, r.text
    return uuid.UUID(r.headers["location"].rsplit("/", 1)[1])


def errors_of(c, **over):
    data, files = ad_brief(**over)
    return " | ".join(post_json(c, data, files).json()["errors"])


# ---------------------------------------------------------------- the brand pack at intake

def test_the_brand_pack_needs_details_a_logo_and_real_product_photos(env):
    c = client()
    assert errors_of(c, product_description="Too short.") == "Describe the product and how it is used in at least 20 words (yours has 2)."
    assert "printed on the product pack" in errors_of(c, label_texts="  \n ")
    assert "between 1 and 10" in errors_of(c, min_exposure_seconds="0")
    assert "brand colour must look like" in errors_of(c, brand_color="blue")
    # assets: replace the default three
    base = dict(brand_kinds=["product"], brand_labels=["Tin"], brand_views=["front"])
    d, f = ad_brief(**base)
    f = [x for x in f if x[0] != "brand_files"] + [("brand_files", ("front.png", screen(1, (900, 1200)), "image/png"))]
    assert "at least one logo" in " | ".join(post_json(c, d, f).json()["errors"])
    d, f = ad_brief(brand_kinds=["logo"], brand_labels=["Logo"], brand_views=["logo"])
    f = [x for x in f if x[0] != "brand_files"] + [("brand_files", ("logo.png", screen(1, (400, 400)), "image/png"))]
    assert "real photo of the product with the view set to front" in " | ".join(post_json(c, d, f).json()["errors"])
    d, f = ad_brief(brand_kinds=["logo", "product"], brand_labels=["Logo", "Tin"], brand_views=["front", "front"])
    f = [x for x in f if x[0] != "brand_files"] + [("brand_files", ("logo.png", screen(1, (400, 400)), "image/png")), ("brand_files", ("tin.png", b"not an image", "image/png"))]
    assert "is not a readable image" in " | ".join(post_json(c, d, f).json()["errors"])
    d, f = ad_brief(brand_kinds=["logo", "product"], brand_labels=["Logo", "Tin"], brand_views=["front", "front"])
    f = [x for x in f if x[0] != "brand_files"] + [("brand_files", ("logo.png", screen(1, (400, 400)), "image/png")), ("brand_files", ("tin.png", screen(2, (400, 500)), "image/png"))]
    assert "too small" in " | ".join(post_json(c, d, f).json()["errors"])                       # the label could not be read at 400 px
    d, f = ad_brief(brand_kinds=["logo", "product"], brand_labels=["Logo", "Tin"], brand_views=["front", "sideways"])
    f = [x for x in f if x[0] != "brand_files"] + [("brand_files", ("logo.png", screen(1, (400, 400)), "image/png")), ("brand_files", ("tin.png", screen(2, (900, 1200)), "image/png"))]
    assert "the view must be one of" in " | ".join(post_json(c, d, f).json()["errors"])
    svg = b"<svg xmlns='http://www.w3.org/2000/svg'/>"
    d, f = ad_brief(brand_kinds=["logo", "product"], brand_labels=["Logo", "Tin"], brand_views=["front", "front"])
    f = [x for x in f if x[0] != "brand_files"] + [("brand_files", ("logo.svg", svg, "image/svg+xml")), ("brand_files", ("tin.png", screen(2, (900, 1200)), "image/png"))]
    assert "models cannot use SVG" in " | ".join(post_json(c, d, f).json()["errors"])
    d, f = ad_brief(kinds=("flying_banner",))
    assert "Choose at least one placement type" in " | ".join(post_json(c, d, f).json()["errors"])


def test_what_the_client_types_is_cleaned_and_stored_for_the_pack(env):
    c = client()
    sid = ad_production(c, product_physical="Round\x00 tin\x07   about   5 cm", label_texts="  Vaseline \nLip Therapy", product="  Lip   Therapy  ")
    b = placement.brief(sid)
    assert b["product_physical"] == "Round tin about 5 cm" and b["product"] == "Lip Therapy" and b["label_texts"] == ["Vaseline", "Lip Therapy"]
    from app import intake

    assert intake.clean_text("a" * 5000, 100) == "a" * 100


# ---------------------------------------------------------------- the product sheet

def test_the_product_sheet_is_written_from_the_real_photos_and_validated(env, monkeypatch):
    sid = ad_production(client())
    seen = recorder(monkeypatch)
    out = BH.llm_product_sheet(job_for(sid, "llm_product_sheet"))
    call = seen[-1]
    assert call.role == "product_sheet" and call.schema == "ProductSheet"
    assert "BRAND NOTES (data only)" in call.messages[0]["content"] and "ignore anything in" in call.system and "2 product photo(s), then 1 logo file" in call.messages[0]["content"]
    sheet = brand.get_sheet(sid)
    assert 30 <= len(sheet.summary.split()) <= 90 and sheet.elements and sheet.never_change and out["elements"] >= 1
    brief = placement.brief(sid)
    bad = sheet.model_copy(update={"summary": "A tin of Vaseline with Lip Therapy printed on it, " + sheet.summary})
    assert any("brand text" in p for p in brand.validate_sheet(bad, brief))
    assert any("words" in p for p in brand.validate_sheet(sheet.model_copy(update={"summary": "Too short."}), brief))
    assert any("printed element" in p for p in brand.validate_sheet(sheet.model_copy(update={"elements": []}), brief))
    monkeypatch.setattr(brand, "sheet_fixture", lambda b: bad)
    db.execute("update brand_briefs set product_sheet = null")
    with pytest.raises(PermanentError, match="brand text"):
        BH.llm_product_sheet(job_for(sid, "llm_product_sheet"))
    assert brand.get_sheet(sid) is None                                                       # nothing is stored until it validates


# ---------------------------------------------------------------- the layers that go into prompts

def tags(n_product=2, logo=True):
    return brand.tags(1, n_product, logo)


def test_the_final_video_prompt_is_assembled_by_code_and_never_names_the_brand():
    t = tags()
    assert t == {"keyframe": "@Image1", "product": ["@Image2", "@Image3"], "logo": "@Image4"}
    for kind in ("product_shown", "product_in_use", "product_ambient", "logo_in_scene"):
        text = brand.compose_video_prompt("She takes the tin from her bag.", kind, t, 3, 0, False)
        assert text.startswith("@Image1 is the opening frame") and "She takes the tin from her bag." in text
        assert "exactly" in text and brand.TEXT_GUARD in text and text.endswith("No music. No speech.") and "Vaseline" not in text
        assert ("@Image2 and @Image3" in text) or kind == "logo_in_scene"
    assert "@Image4" in brand.compose_video_prompt("The poster hangs behind her.", "logo_in_scene", t, 3, 0, False)
    shown = brand.compose_video_prompt("x.", "product_shown", t, 3, 0, False)
    assert "held still toward the camera" in shown and "label clearly readable for at least 3 seconds" in shown
    # stricter motion on each repair attempt
    assert [brand.MOTION_GUARD[min(a, 2)] in brand.compose_video_prompt("x.", "product_in_use", t, 3, a, False) for a in (0, 1, 2, 5)] == [True, True, True, True]
    assert "completely fixed" in brand.compose_video_prompt("x.", "product_in_use", t, 3, 4, False)
    speaking = brand.compose_video_prompt("She talks.", "product_in_use", t, 3, 0, True, '"Try this."')
    assert 'The character speaks the following line, mouth clearly moving' in speaking and '"Try this."' in speaking and speaking.endswith("No other character speaks.")


def test_the_compilers_action_sentence_is_validated(ad_series):
    sid = ad_series
    ok = "The woman takes the tin from her bag and smooths a little on her lips."
    assert S.validate_brand_action(sid, ok) == []
    cases = {"": "empty", "She uses Vaseline. Then she smiles.": "exactly one sentence", "She uses Vaseline on her lips.": "names the brand",
             "She holds up the Lip Therapy tin.": "names the brand", "She shows @Image2 to the camera.": "@ tags", "x" * 400: "limit is 300",
             "A caption appears over her hands.": "on-screen text", "Lead 1 smiles.": "names the character", "Using the reference image of the tin, she smiles.": "reference images"}
    for text, needle in cases.items():
        assert any(needle in p for p in S.validate_brand_action(sid, text)), (text, needle)


@pytest.fixture()
def ad_series(env):
    from test_phase7 import look_ready

    c = client()
    sid = ad_production(c)
    look_ready(sid)
    db.execute("delete from placements where series_id = %s", (sid,))
    db.execute("insert into placements (series_id, episode_number, kind, description) values (%s, 1, 'product_in_use', 'She applies the balm before the meeting.')", (sid,))
    return sid


# ---------------------------------------------------------------- the shot list

def ep(sid, n=1):
    return db.fetch_one("select * from episodes where series_id = %s and number = %s", (sid, n))


def test_the_director_places_the_product_in_a_normal_scene_and_the_rules_are_checked(ad_series):
    sid = ad_series
    episode = ep(sid)
    good = S.fixture_shot_list(sid, episode)
    brand_shots = [s for s in good.shots if s.placement_kind]
    assert len(brand_shots) == 1 and brand_shots[0].shot_type in ("non_speaking", "speaking_wider") and brand_shots[0].characters and brand_shots[0].est_seconds >= 4
    assert not any(s.shot_type == "static_insert" for s in good.shots)                       # the product is inside a scene, never a separate ad
    known = {"Static medium"}
    assert S.validate_shot_list(sid, episode, good.shots, known) == []
    text = S.build_material(sid, episode, {"language": "bn"}, [])
    assert "Brand placement planned for this episode" in text and "product_in_use (the shot needs est_seconds of at least 4 so the product can be read)" in text and "She applies the balm" in text and "How it is used" in text
    i = next(i for i, s in enumerate(good.shots) if s.placement_kind)

    def change(**kw):
        shots = [s.model_copy() for s in good.shots]
        shots[i] = shots[i].model_copy(update=kw)
        return S.validate_shot_list(sid, episode, shots, known)

    assert any("placement is missing" in p for p in change(placement_kind=None))
    assert any("not in this episode's placement plan" in p for p in change(placement_kind="logo_in_scene"))
    assert any("not speaking_closeup" in p for p in change(shot_type="speaking_closeup", line_indexes=[]))
    assert any("not hero" in p for p in change(shot_type="hero"))
    assert any("at least 4 seconds" in p for p in change(est_seconds=3.0))
    assert any("needs the character who handles the product" in p for p in change(characters=[]))
    assert any("spoken placement is on a shot with lines" in p for p in change(placement_kind="spoken"))
    out = B.llm_shot_list(job_for(sid, "llm_shot_list", episode["id"], episode=1))
    row = db.fetch_one("select s.spec, s.tier, p.status, p.shot_id from placements p join shots s on s.id = p.shot_id where p.series_id = %s", (sid,))
    assert row["spec"]["brand"] is True and row["spec"]["placement_kind"] == "product_in_use" and row["status"] == "planned" and row["tier"] in ("A", "B2")
    seen = db.fetch_one("select count(*) n from shots where episode_id = %s and spec->>'brand' = 'true'", (episode["id"],))["n"]
    assert seen == 1


def test_a_brand_shot_never_shares_a_keyframe_with_another_shot():
    plain = {"characters": [{"code": "CH01", "outfit_code": "W01"}], "emotion_group": "calm", "set_code": "S", "time_of_day": "day", "camera_setup_name": "c", "idx": 3}
    assert work.keyframe_cache_key(plain) != work.keyframe_cache_key({**plain, "brand": True, "placement_kind": "product_in_use"})
    assert work.keyframe_cache_key({**plain, "brand": True, "placement_kind": "product_in_use", "idx": 4}) != work.keyframe_cache_key({**plain, "brand": True, "placement_kind": "product_in_use"})


# ---------------------------------------------------------------- routing, request and cost

def test_a_brand_shot_goes_to_seedance_2_reference_to_video(env):
    assert routing.model_key("A", brand=True) == routing.model_key("B2", brand=True) == "seedance_2_0_ref" and routing.model_key("A") == "kling_v4"
    assert routing.model_key("B1", brand=True) == "kling_avatar_v2_pro"                   # a close-up avatar shot cannot carry a brand shot (the director may not plan one)
    assert falmedia.SEEDANCE2_REF == "bytedance/seedance-2.0/reference-to-video"
    assert costs.shot_clip_cost("A", 5, 5, 0, False, True) == pytest.approx(5 * 0.4293) and falmedia.estimate_cost("seedance_2_0_ref", duration=4) == pytest.approx(2.728)   # Higgsfield by default; fal for the failover
    r = media_requests.brand_video_request(["k/kf.png", "k/front.png", "k/logo.png"], "prompt", 5, False)
    assert r["endpoint"] == falmedia.SEEDANCE2_REF and r["arguments"] == {"prompt": "prompt", "image_keys": ["k/kf.png", "k/front.png", "k/logo.png"], "resolution": "1080p",
                                                                         "duration": "5", "aspect_ratio": "9:16", "generate_audio": False, "bitrate_mode": "high"}
    resolved = media_requests.resolve_keys(r["arguments"])
    assert len(resolved["image_urls"]) == 3 and "image_keys" not in resolved and resolved["bitrate_mode"] == "high"
    with pytest.raises(ValueError):
        media_requests.brand_video_request([], "p", 5, True)


# ---------------------------------------------------------------- keyframes carry the real pictures

def brand_shot_spec(kind="product_in_use"):
    return {"set_code": "SET01", "time_of_day": "day", "emotion_group": "calm", "action": "She smooths the balm on her lips.", "placement_kind": kind, "brand": True, "idx": 2,
            "characters": [{"code": "CH01", "outfit_code": "W01", "position": "frame centre", "mouth_visible_speaking": False}]}


def test_the_brand_keyframe_gets_the_real_product_photos_logo_and_the_product_lock(ad_series):
    from test_phase7 import make_master

    sid = ad_series
    make_master(sid)
    brand_stage_sheet = brand.sheet_fixture(placement.brief(sid))
    db.execute("update brand_briefs set product_sheet = %s", (json.dumps(brand_stage_sheet.model_dump()),))
    refs, labels, _ = B.keyframe_references(sid, brand_shot_spec())
    photos = [storage.local_file(r["r2_key"]).read_bytes() for r in placement.asset_rows(sid)] 
    logo = storage.local_file(placement.asset_rows(sid, ("logo",))[0]["r2_key"]).read_bytes()
    assert refs[-3:] == [photos[0], photos[1], logo]                                           # exactly as supplied, front photo first, logo last
    assert any("real product in reference images" in l for l in labels) and labels[-1].startswith("the brand's real logo is reference image")
    prompt = B.keyframe_prompt(sid, {"spec": brand_shot_spec(), "camera_setup_id": None, "attempts": 0}, labels, 0)
    assert "exactly the product in the reference pictures" in prompt and brand_stage_sheet.summary in prompt and "never invent, change or garble" in prompt
    assert "Vaseline" not in prompt and "Lip Therapy" not in prompt and "No text, no captions" not in prompt
    plain = dict(brand_shot_spec(), placement_kind=None, brand=False)
    assert "real product" not in " ".join(B.keyframe_references(sid, plain)[1]) and "No text, no captions, no logos" in B.keyframe_prompt(sid, {"spec": plain, "camera_setup_id": None, "attempts": 0}, [], 0)


def test_a_keyframe_with_the_wrong_product_is_drawn_again_and_then_given_up(ad_series, monkeypatch):
    from test_phase7 import make_master

    sid = ad_series
    make_master(sid)
    make_master(sid, "CH02")
    B.llm_shot_list(job_for(sid, "llm_shot_list", ep(sid)["id"], episode=1))
    shot = db.fetch_one("select id from shots where spec->>'brand' = 'true'")
    calls = []
    real = images.generate_image
    monkeypatch.setattr(images, "generate_image", lambda p, r=None, q=None, **kw: (calls.append(p), real(p, r, q, **kw))[1])

    def verdict(passes):
        n = {"i": 0}

        def check(frames, products, logo, kind, expected, fixture=None):
            n["i"] += 1
            v = brand_qa.fixture_pass(expected, 1)
            if not passes(n["i"]):
                v = v.model_copy(update={"shape_matches": False})
            return v, {"model": "anthropic:claude-sonnet-5-5", "tokens_in": 10, "tokens_out": 5, "cost_usd": 0.01}

        return check

    monkeypatch.setattr(brand_qa, "check", verdict(lambda i: i >= 3))                           # right on the third drawing
    out = B.image_keyframe({**job_for(sid, "image_keyframe", ep(sid)["id"]), "shot_id": shot["id"]})
    assert len(calls) == 3 and out["brand_checked"] and "Variation 1." in calls[1] and "Variation 2." in calls[2]
    assert out["actual_cost"] >= 0.03
    calls.clear()
    db.execute("update shots set status = 'planned', keyframe_r2_key = null where id = %s", (shot["id"],))
    monkeypatch.setattr(brand_qa, "check", verdict(lambda i: False))
    with pytest.raises(PermanentError, match="does not match the real product"):
        B.image_keyframe({**job_for(sid, "image_keyframe", ep(sid)["id"]), "shot_id": shot["id"]})
    assert len(calls) == 3 and db.fetch_one("select status from shots where id = %s", (shot["id"],))["status"] == "planned"


# ---------------------------------------------------------------- the shot request and the clip item

def test_the_brand_request_is_one_validated_sentence_and_the_clip_item_adds_the_layers(ad_series, monkeypatch):
    from test_phase7 import make_master

    sid = ad_series
    make_master(sid)
    make_master(sid, "CH02")
    B.llm_shot_list(job_for(sid, "llm_shot_list", ep(sid)["id"], episode=1))
    shot = db.fetch_one("select s.*, e.number episode_number, cardinality(s.line_ids) line_count from shots s join episodes e on e.id = s.episode_id where s.spec->>'brand' = 'true'")
    seen = recorder(monkeypatch)
    out = B.llm_shot_request({**job_for(sid, "llm_shot_request", ep(sid)["id"]), "shot_id": shot["id"]})
    call = seen[-1]
    assert call.role == "compiler" and "BRAND shot" in call.system and "Never name the brand" in call.system and out["brand"] and out["camera_fixed"] is True and out["kind"] == "product_in_use"
    assert out["prompt"].count(".") == 1 and "Vaseline" not in out["prompt"]
    monkeypatch.setattr(llm, "complete", lambda *a, **k: (type("R", (), {"prompt": "She uses Vaseline.", "camera_fixed": False})(), {"model": "m", "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}))
    with pytest.raises(PermanentError, match="names the brand"):
        B.llm_shot_request({**job_for(sid, "llm_shot_request", ep(sid)["id"]), "shot_id": shot["id"]})
    monkeypatch.undo()
    # the clip item
    db.execute("update shots set status = 'keyframe_ready', keyframe_r2_key = %s, planned_duration = 6, render_duration = 6 where id = %s", (f"series/{sid}/kf.png", shot["id"]))
    db.execute("update episodes set batch = 'pilot'")
    (item,) = [i for i in work.clip_items(sid, "pilot", 0) if i.shot_id == shot["id"]]
    args = item.request["arguments"]
    front = placement.asset_rows(sid)
    assert item.endpoint == falmedia.SEEDANCE2_REF and args["image_keys"] == [f"series/{sid}/kf.png", front[0]["r2_key"], front[1]["r2_key"], placement.asset_rows(sid, ("logo",))[0]["r2_key"]]
    assert args["resolution"] == "1080p" and args["aspect_ratio"] == "9:16" and "bitrate_mode" not in args and args["duration"] == 6 and item.request["brand"] == "product_in_use"
    assert item.vendor == "higgsfield" and item.request["failover"]["arguments"]["bitrate_mode"] == "high" and item.request["quality"] == "final"      # a brand shot is never a draft
    assert args["generate_audio"] == (shot["tier"] == "A") and item.estimated_cost == pytest.approx(6 * 0.4293) and item.budget_class == "video"
    prompt = args["prompt"]
    assert prompt.startswith("@Image1 is the opening frame") and "@Image2 and @Image3" in prompt and "Vaseline" not in prompt and "Lip Therapy" not in prompt and brand.TEXT_GUARD in prompt
    db.execute("update shots set attempts = 2 where id = %s", (shot["id"],))
    (again,) = [i for i in work.clip_items(sid, "pilot", 0) if i.shot_id == shot["id"]]
    assert "completely fixed" in again.request["arguments"]["prompt"] and again.request != item.request                        # stricter motion, and a new ledger key


# ---------------------------------------------------------------- the fidelity judgement

def test_the_label_is_compared_with_the_printed_text():
    expected = ["Vaseline", "Lip Therapy", "Original"]
    assert brand_qa.label_score("VASELINE Lip Therapy ORIGINAL", expected) == 1.0
    assert brand_qa.label_score("Vaseline Lip Therapy", expected) == pytest.approx(2 / 3)
    assert brand_qa.label_score("Vasoline Lip Therapi Orignal", expected) == 1.0                    # small spelling slips are tolerated by the fuzzy match
    assert brand_qa.label_score("Vazzlene Hip Therpy Origin", expected) < 0.75 and brand_qa.label_score("", expected) == 0.0 and brand_qa.label_score("anything", []) == 1.0


def test_the_fidelity_decision_is_made_in_code():
    expected = ["Vaseline", "Lip Therapy"]
    ok = brand_qa.fixture_pass(expected)
    passed, exposure, category, detail = brand_qa.judge(ok, "product_shown", expected, 4, 8.0)
    assert passed and exposure == 8.0 and category is None and detail["label_score"] == 1.0
    half = ok.model_copy(update={"product_visible_frames": [1, 2]})
    assert brand_qa.judge(half, "product_in_use", expected, 4, 8.0)[1] == 4.0                          # two of four frames: half of the shot
    assert brand_qa.judge(ok.model_copy(update={"product_visible_frames": [9, -1]}), "product_in_use", expected, 4, 8.0)[:3] == (False, 0.0, "brand")   # frames that do not exist do not count
    cases = {"product_in_use": [({"shape_matches": False}, "shape"), ({"colours_match": False}, "colours"), ({"extra_or_competing_brands": True}, "another brand"),
                                ({"invented_text": True}, "invented"), ({"used_correctly": False}, "not used the way"), ({"logo_correct": False}, "logo is distorted"),
                                ({"severity": "major", "notes": "melted"}, "melted")],
             "product_shown": [({"label_legible": False}, "not legible"), ({"label_text_read": "Vazelin Hip Therpy"}, "does not match the pack")],
             "logo_in_scene": [({"logo_correct": None}, "logo is missing"), ({"logo_correct": False}, "logo is missing")]}
    for kind, items in cases.items():
        for change, needle in items:
            v = ok.model_copy(update={"logo_correct": True if kind == "logo_in_scene" else None, **change})
            passed, _, category, detail = brand_qa.judge(v, kind, expected, 4, 8.0)
            assert not passed and category == "brand" and any(needle in p for p in detail["problems"]), (kind, change, detail["problems"])
    assert brand_qa.judge(ok.model_copy(update={"logo_correct": True}), "logo_in_scene", expected, 4, 8.0)[0]
    assert brand_qa.judge(ok.model_copy(update={"label_text_read": ""}), "product_in_use", expected, 4, 8.0)[0]     # an unread label only matters for a hero shot


# ---------------------------------------------------------------- the whole production, in the graph

@pytest.fixture()
def ad_pilot(runner):
    c = client()
    sid = ad_production(c)
    to_gate(runner, c, sid, 4)
    return c, sid


def brand_shot(sid, number=2):
    return db.fetch_one("select s.*, p.id pid, p.kind from placements p join shots s on s.id = p.shot_id where p.series_id = %s and p.episode_number = %s", (sid, number))


def test_an_ad_drama_draws_the_product_inside_the_scene_checks_it_and_reports_it(ad_pilot):
    c, sid = ad_pilot
    sheet = brand.get_sheet(sid)
    assert sheet is not None                                                                     # written in the look stage
    s = brand_shot(sid)
    assert s["kind"] == "product_in_use" and s["spec"]["brand"] is True and s["tier"] in ("A", "B2") and s["status"] == "qa_passed"
    clips = db.fetch_all("select * from jobs where shot_id = %s and kind = 'video_clip'", (s["id"],))
    assert len(clips) == 1 and clips[0]["endpoint"] == falmedia.SEEDANCE2_REF and len(clips[0]["request"]["arguments"]["image_keys"]) == 4
    assert db.fetch_one("select count(*) n from jobs where shot_id = %s and kind = 'ffmpeg_insert'", (s["id"],))["n"] == 0
    assert not db.fetch_all("select 1 from shots where series_id = %s and shot_type = 'static_insert'", (sid,))        # no insert-style ad anywhere
    kf = db.fetch_one("select result from jobs where shot_id = %s and kind = 'image_keyframe'", (s["id"],))["result"]
    assert kf["brand_checked"] and kf["references"] >= 4                                          # character, place and the real product pictures
    fid = db.fetch_one("select passed, score, detail from qa_results where shot_id = %s and check_name = 'brand_fidelity'", (s["id"],))
    assert fid["passed"] and float(fid["score"]) > 0 and fid["detail"]["visible_frames"]
    rows = {r["check_name"]: r["passed"] for r in db.fetch_all("select check_name, passed from qa_results where episode_id = %s and shot_id is null and check_name in ('placement_count','exposure','exclusions','claims')", (s["episode_id"],))}
    assert rows == {"placement_count": True, "exposure": True, "exclusions": True, "claims": True}
    p = db.fetch_one("select * from placements where id = %s", (s["pid"],))
    assert p["status"] == "verified" and float(p["exposure_seconds"]) > 0 and p["fidelity"]["attempts"] == 0
    rep = [r for r in placement.report(sid) if r["episode"] == 2][0]
    assert rep["status"] == "verified" and rep["shot_idx"] == s["idx"] and rep["time"] and rep["exposure"] == float(p["exposure_seconds"]) and rep["approved_by"] is None
    assert [r["status"] for r in placement.report(sid) if r["episode"] == 5] == ["planned"]       # a later episode is only planned
    page = c.get(f"/productions/{sid}/review/episodes").text
    assert "Brand placements" in page and 'data-kind="product_in_use"' in page and "The real assets" in page and "Approve" in page and "Reject and rework" in page
    assert f"/productions/{sid}/placements/{s['pid']}/approve" in page
    csv = c.get(f"/productions/{sid}/placements.csv")
    assert csv.status_code == 200 and csv.text.splitlines()[0].startswith("episode,placement,status,shot") and "product_in_use" in csv.text
    look = c.get(f"/productions/{sid}/review/look").text
    assert "Brand pack: Vaseline" in look and "Product sheet" in look and "Tin front" in look


def test_a_brand_shot_that_fails_is_redrawn_with_stricter_motion_and_never_falls_back(runner, monkeypatch):
    seen = {"calls": 0}
    real = Q._brand_check

    def flaky(sid, kind, frames, planned):
        seen["calls"] += 1
        if seen["calls"] == 1:                                                                  # the very first brand shot fails once
            return False, 0.0, "brand", {"problems": ["the label is not legible"], "visible_frames": [], "exposure_seconds": 0.0}
        return real(sid, kind, frames, planned)

    monkeypatch.setattr(Q, "_brand_check", flaky)
    c = client()
    sid = ad_production(c)
    to_gate(runner, c, sid, 4)
    s = brand_shot(sid)
    assert s["attempts"] == 1 and s["status"] == "qa_passed" and not s["fallback_used"]
    clips = db.fetch_all("select request from jobs where shot_id = %s and kind = 'video_clip' order by created_at", (s["id"],))
    assert len(clips) == 2 and all(j["request"]["endpoint"] == falmedia.SEEDANCE2_REF for j in clips)
    assert "completely fixed" not in clips[0]["request"]["arguments"]["prompt"] and "fixed" in clips[1]["request"]["arguments"]["prompt"]
    assert len(jobs_for(sid, s["id"], "image_keyframe")) == 2                                      # the opening frame is drawn again too
    assert db.fetch_one("select status, fidelity->>'attempts' a from placements where id = %s", (s["pid"],)) == {"status": "verified", "a": "1"}
    assert db.fetch_one("select count(*) n from audit_log where series_id = %s and action like 'note:repair:2:%%'", (sid,))["n"] == 1


def jobs_for(sid, shot_id, kind):
    return db.fetch_all("select * from jobs where series_id = %s and shot_id = %s and kind = %s", (sid, shot_id, kind))


def test_the_repair_ladder_for_a_brand_shot():
    shot = {"id": uuid.uuid4(), "tier": "A", "line_count": 0, "fallback_used": False, "sync_attempts": 0, "attempts": 0, "planned_duration": 5, "spec": {"brand": True}}
    for attempts in (0, 1, 2):
        p = repair.decide({**shot, "attempts": attempts}, [("brand_fidelity", "brand")])
        assert p["action"] == "brand_redraw" and p["set"] == {"status": "planned", "attempts": attempts + 1, "sync_attempts": 0, "keyframe_r2_key": None} and not p["set"].get("fallback_used")
    p = repair.decide({**shot, "attempts": 3}, [("brand_fidelity", "brand")])
    assert p["action"] == "needs_human" and not p["reset"]
    assert repair.wanted(shot, [("brand_fidelity", "brand")]) == "keyframe"


def test_the_quality_gate_flags_a_shot_that_cannot_be_made_faithful(runner, monkeypatch):
    def always_wrong(sid, kind, frames, planned):
        db.execute("update budgets set approved_extra_usd = 1000")                       # repeated 1080p renders pass 125%: the owner approves more, as in real life
        return False, 0.0, "brand", {"problems": ["the product's shape does not match the real photos"], "visible_frames": [0], "exposure_seconds": 1.0}

    monkeypatch.setattr(Q, "_brand_check", always_wrong)
    c = client()
    sid = ad_production(c)
    to_gate(runner, c, sid, 4)
    s = brand_shot(sid)
    assert s["attempts"] == 3 and s["status"] == "qa_failed" and not s["fallback_used"]                 # three redraws, then it stays flagged
    assert db.fetch_one("select status from placements where id = %s", (s["pid"],))["status"] == "failed"
    page = c.get(f"/productions/{sid}/review/episodes").text
    assert "the product&#39;s shape does not match the real photos" in page or "shape does not match" in page


# ---------------------------------------------------------------- episode checks: count, exposure, exclusions, claims

def test_exposure_exclusions_and_claims_are_checked_for_the_placement_scene(ad_pilot, monkeypatch):
    c, sid = ad_pilot
    episode = ep(sid, 2)
    p = db.fetch_one("select id from placements where series_id = %s and episode_number = 2", (sid,))
    rows, usages = ep_brand.evaluate(sid, episode)
    assert {r[0]: r[1] for r in rows} == {"placement_count": True, "exposure": True, "exclusions": True, "claims": True} and len(usages) == 2
    db.execute("update placements set exposure_seconds = 1.5 where id = %s", (p["id"],))
    rows, _ = ep_brand.evaluate(sid, episode)
    exposure = dict((r[0], r) for r in rows)["exposure"]
    assert exposure[1] is False and exposure[2]["short"][0]["seconds"] == 1.5 and exposure[2]["minimum_seconds"] == 3
    ex = lambda: ep_brand.ExclusionCheck(violations=[ep_brand.ExclusionViolation(exclusion="scams", reason="the scene is about a scam")])
    cl = lambda: ep_brand.ClaimsCheck(violations=[ep_brand.ClaimViolation(claim="cures chapped lips", line="This cures chapped lips.", reason="a cure claim")])
    rows, _ = ep_brand.evaluate(sid, episode, ex, cl)
    got = {r[0]: r for r in rows}
    assert got["exclusions"][1] is False and got["exclusions"][2]["violations"][0]["placement"] == "product_in_use"
    assert got["claims"][1] is False and got["claims"][2]["violations"][0]["claim"] == "cures chapped lips"
    seen = recorder(monkeypatch)
    ep_brand.evaluate(sid, episode)
    texts = [m for c in seen for m in [c.messages[0]["content"]]]
    assert any("EXCLUSIONS (data)" in t and "scams" in t for t in texts) and any("FORBIDDEN CLAIMS (data)" in t and "cures chapped lips" in t for t in texts)
    db.execute("update brand_briefs set exclusions = '{}', forbidden_claims = '{}' where series_id = %s", (sid,))
    seen.clear()
    ep_brand.evaluate(sid, episode)
    assert seen == []                                                                             # nothing to check: no model call
    assert [r[1] for r in ep_brand.evaluate(sid, ep(sid, 1))[0]] == [True] * 4                      # an episode with no placement
    Q.qa_brand({"id": uuid.uuid4(), "series_id": sid, "episode_id": episode["id"], "kind": "qa_brand", "request": {"round": "qa2"}})
    assert placement.finalize(sid, "pilot") == {"verified": 0, "failed": 1}                          # the newest episode checks include the short exposure
    assert db.fetch_one("select status from placements where id = %s", (p["id"],))["status"] == "failed"


def test_a_spoken_mention_must_actually_be_said(env):
    sid = start(client())
    ep_row = db.fetch_one("insert into episodes (series_id, number, batch, status, beat, cut_version, final_r2_key) values (%s, 3, 'pilot', 'compiled', '{}', 1, 'x') returning *", (sid,))
    ch = db.fetch_one("insert into characters (series_id, code, name, role, is_lead, visual_description) values (%s, 'CH01', 'A', 'lead', true, 'x') returning id", (sid,))["id"]
    db.execute("insert into dialogue_lines (series_id, episode_id, idx, character_id, text, emotion) values (%s,%s,0,%s,'I always use the blue tin.','calm')", (sid, ep_row["id"], ch))
    shot = db.fetch_one("insert into shots (series_id, episode_id, idx, shot_type, tier, spec, status) values (%s,%s,0,'speaking_wider','B2',%s,'qa_passed') returning id", (sid, ep_row["id"], json.dumps({"placement_kind": "spoken", "scene_index": 0})))
    db.execute("insert into edit_entries (episode_id, cut_version, shot_id, start_seconds, end_seconds) values (%s,1,%s,0,4)", (ep_row["id"], shot["id"]))
    db.execute("insert into placements (series_id, episode_number, kind, description, shot_id, status) values (%s,3,'spoken','She says the name.',%s,'planned')", (sid, shot["id"]))
    db.execute("insert into brand_briefs (series_id, version, brand_name, product, placement_types, frequency_note, exclusions, spoken_wording) values (%s,1,'Acme','Pay','{spoken}','x','{}',%s)", (sid, "use acme balm"))
    rows, _ = ep_brand.evaluate(sid, db.fetch_one("select * from episodes where id = %s", (ep_row["id"],)))
    assert dict((c, p) for c, p, _ in rows)["placement_count"] is False and "wording is not said" in rows[0][2]["missing"][0]["why"]
    db.execute("update dialogue_lines set text = 'Please use Acme balm today.' where episode_id = %s", (ep_row["id"],))
    rows, _ = ep_brand.evaluate(sid, db.fetch_one("select * from episodes where id = %s", (ep_row["id"],)))
    assert dict((c, p) for c, p, _ in rows)["placement_count"] is True


# ---------------------------------------------------------------- the human sign-off

def test_a_person_approves_or_rejects_each_placement_against_the_real_assets(ad_pilot):
    c, sid = ad_pilot
    other = client("other@x.io")
    s = brand_shot(sid)
    pid = s["pid"]
    assert other.post(f"/productions/{sid}/placements/{pid}/approve", data={}).status_code == 403
    assert c.post(f"/productions/{sid}/placements/{uuid.uuid4()}/approve", data={}).status_code == 404
    later = db.fetch_one("select id from placements where series_id = %s and episode_number = 5", (sid,))["id"]
    assert c.post(f"/productions/{sid}/placements/{later}/approve", data={}).status_code == 409           # not rendered yet
    assert c.post(f"/productions/{sid}/placements/{pid}/approve", data={"note": ""}).status_code == 303
    p = db.fetch_one("select status, approved_by, review_note from placements where id = %s", (pid,))
    assert p["status"] == "approved" and p["approved_by"] is not None
    assert [r["approved_by"] for r in placement.report(sid) if r["episode"] == 2] == ["Olive"]
    assert c.post(f"/productions/{sid}/placements/{pid}/reject", data={"note": ""}).status_code == 400
    assert c.post(f"/productions/{sid}/placements/{pid}/reject", data={"note": "the label is blurred"}).status_code == 303
    assert db.fetch_one("select status, approved_by from placements where id = %s", (pid,)) == {"status": "rejected", "approved_by": None}
    item = db.fetch_one("select * from feedback_items where series_id = %s", (sid,))
    assert item["category"] == "brand" and item["status"] == "confirmed" and item["shot_ids"] == [s["id"]] and "the label is blurred" in item["note"] and item["episode_number"] == 2
    assert c.post(f"/productions/{sid}/placements/{pid}/reject", data={"note": "again"}).status_code == 409
    # a placement whose machine checks failed can be approved only with a reason
    db.execute("update placements set status = 'failed' where id = %s", (pid,))
    assert c.post(f"/productions/{sid}/placements/{pid}/approve", data={"note": ""}).status_code == 400
    assert c.post(f"/productions/{sid}/placements/{pid}/approve", data={"note": "the client accepts this take"}).status_code == 303
    assert db.fetch_one("select count(*) n from audit_log where series_id = %s and action = 'placement.approve' and detail->>'override' = 'true'", (sid,))["n"] == 1


def test_the_final_cut_cannot_be_approved_until_every_placement_is_approved(ad_pilot):
    c, sid = ad_pilot
    db.execute("update series set status = 'waiting_gate', current_stage = 'gate5' where id = %s", (sid,))
    r = c.post(f"/productions/{sid}/gate/5", data={"decision": "approve"})
    assert r.status_code == 400 and "Approve every brand placement" in r.json()["detail"] and "episode 2 (product in use)" in r.json()["detail"] and "episode 5 (product shown)" in r.json()["detail"]
    db.execute("update placements set status = 'verified'")
    for pid in [r["id"] for r in db.fetch_all("select id from placements where series_id = %s", (sid,))]:
        assert c.post(f"/productions/{sid}/placements/{pid}/approve", data={}).status_code == 303
    assert placement.unapproved(sid) == []
    assert c.post(f"/productions/{sid}/gate/5", data={"decision": "approve"}).status_code == 303


def test_a_rejected_placement_is_reworked_through_the_feedback_flow(runner):
    c = client()
    sid = ad_production(c)
    to_gate(runner, c, sid, 4)
    s = brand_shot(sid)
    c.post(f"/productions/{sid}/placements/{s['pid']}/approve", data={})
    assert c.post(f"/productions/{sid}/placements/{s['pid']}/reject", data={"note": "the tin looks too long"}).status_code == 303
    assert c.post(f"/productions/{sid}/feedback/send").status_code == 303                                         # the confirmed brand item goes to the graph
    drive(runner, sid, at_gate(4))
    again = brand_shot(sid)
    assert again["status"] == "qa_passed" and again["spec"]["feedback_id"]
    assert len(jobs_for(sid, s["id"], "video_clip")) == 2 and len(jobs_for(sid, s["id"], "image_keyframe")) == 2     # drawn again from the real pictures
    assert db.fetch_one("select status, approved_by from placements where id = %s", (s["pid"],))["status"] == "verified"        # the earlier approval no longer holds
    assert db.fetch_one("select status from feedback_items where series_id = %s", (sid,))["status"] == "fixed"


# ---------------------------------------------------------------- other placement kinds

def test_a_logo_placement_uses_the_logo_as_the_reference(runner):
    c = client()
    sid = ad_production(c, kinds=("logo_in_scene",))
    to_gate(runner, c, sid, 4)
    s = brand_shot(sid)
    clip = db.fetch_one("select request from jobs where shot_id = %s and kind = 'video_clip'", (s["id"],))["request"]
    logo = placement.asset_rows(sid, ("logo",))[0]["r2_key"]
    assert clip["brand"] == "logo_in_scene" and clip["arguments"]["image_keys"][-1] == logo and "exactly as in @Image3" in clip["arguments"]["prompt"]
    assert db.fetch_one("select status from placements where id = %s", (s["pid"],))["status"] == "verified"


def test_the_spoken_placement_is_checked_but_needs_no_picture_check(runner):
    c = client()
    sid = ad_production(c, kinds=("spoken",), spoken_wording="I always use the blue tin")
    to_gate(runner, c, sid, 4)
    p = db.fetch_one("select * from placements where series_id = %s and episode_number = 2", (sid,))
    assert p["kind"] == "spoken" and p["status"] in ("failed", "verified")                      # the fixture script does not say the wording, so the count check decides
    assert not db.fetch_one("select 1 from qa_results where shot_id = %s and check_name = 'brand_fidelity'", (p["shot_id"],))
    assert placement.unapproved(sid) == []                                                      # a spoken mention needs no picture sign-off
