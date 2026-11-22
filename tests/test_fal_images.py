"""Images and embeddings through fal, the model per purpose, the Look gate's Nano Banana Pro redo, and the reviewer's words reaching the models."""
import io
import json
import types

import pytest
from helpers import at_gate, drive
from PIL import Image
from test_phase5 import client, env, gate, runner, start, to_gate, voices  # noqa: F401 (fixtures)
from test_phase11 import ad_production
from test_phase10 import user_id

from app import db, decisions, feedback
from app.config import clear_config_cache
from app.jobs.errors import PermanentError
from app.jobs.handlers import look_handlers
from app.vendors import images, llm


def png_bytes(size):
    buf = io.BytesIO()
    Image.new("RGB", size, (90, 120, 150)).save(buf, "PNG")
    return buf.getvalue()


class FakeFal:
    """Stands in for fal_client.subscribe and the download of the result."""

    def __init__(self, monkeypatch, out_size=(1152, 2048)):
        self.calls = []
        self.out_size = out_size
        monkeypatch.setenv("DRY_RUN", "0")
        import fal_client

        monkeypatch.setattr(fal_client, "subscribe", self.subscribe)
        monkeypatch.setattr(images.httpx, "get", lambda url, **kw: types.SimpleNamespace(status_code=200, text="", content=png_bytes(self.out_size)))

    def subscribe(self, endpoint, arguments=None, **kw):
        self.calls.append((endpoint, arguments))
        return {"images": [{"url": "https://fal.media/x.png", "width": self.out_size[0], "height": self.out_size[1]}]}


# ---------------------------------------------------------------- images through fal

def test_gpt_image_through_fal_uses_edit_with_references_and_text_without(env, monkeypatch):
    fal = FakeFal(monkeypatch)
    png, usage = images.generate_image("A portrait.", references=[png_bytes((900, 1200)), png_bytes((600, 600))])
    endpoint, args = fal.calls[-1]
    assert endpoint == "openai/gpt-image-2.5/flare/edit" and args["image_size"] == {"width": 1152, "height": 2048} and args["quality"] == "medium"
    assert len(args["image_urls"]) == 2 and all(u.startswith("data:image/jpeg;base64,") for u in args["image_urls"])
    assert Image.open(io.BytesIO(png)).size == (1080, 1920) and usage["vendor"] == "fal" and usage["model_key"] == "gpt_image_flare" and usage["cost_usd"] == pytest.approx(0.0186)
    _, usage = images.generate_image("A street at dusk.")
    endpoint, args = fal.calls[-1]
    assert endpoint == "openai/gpt-image-2.5/flare/text-to-image" and "image_urls" not in args and usage["cost_usd"] == pytest.approx(0.0104)


def test_nano_banana_pro_through_fal_asks_for_9_16_and_is_cropped_to_exact_size(env, monkeypatch):
    fal = FakeFal(monkeypatch, out_size=(1536, 2752))                    # not exactly 9:16
    png, usage = images.generate_image("A portrait.", references=[png_bytes((700, 700))], model="nano_banana_pro")
    endpoint, args = fal.calls[-1]
    assert endpoint == "fal-ai/nano-banana-pro/edit" and args["aspect_ratio"] == "9:16" and args["resolution"] == "2K" and "image_size" not in args and "quality" not in args
    assert Image.open(io.BytesIO(png)).size == (1080, 1920) and usage["cost_usd"] == 0.15 and usage["model_key"] == "nano_banana_pro"
    images.generate_image("A street.", model="nano_banana_pro")
    assert fal.calls[-1][0] == "fal-ai/nano-banana-pro"


def test_reference_limits_and_empty_results_are_refused(env, monkeypatch):
    fal = FakeFal(monkeypatch)
    refs = [png_bytes((300, 300))] * 15
    with pytest.raises(PermanentError, match="at most 14"):
        images.generate_image("x", references=refs, model="nano_banana_pro")
    images.generate_image("x", references=refs)                          # GPT Image takes up to 16
    import fal_client

    monkeypatch.setattr(fal_client, "subscribe", lambda *a, **k: {"images": []})
    with pytest.raises(PermanentError, match="no image"):
        images.generate_image("x")
    assert len(fal.calls) == 1


def test_the_model_follows_the_purpose_and_an_explicit_choice_wins(env):
    assert images.model_for("master") == "gpt_image_flare" and images.model_for("keyframe") == "gpt_image_flare"
    assert images.model_for("brand_keyframe") == "nano_banana_pro"            # ad-placement shots
    assert images.model_for("master", "nano_banana_pro") == "nano_banana_pro"
    with pytest.raises(PermanentError):
        images.model_for("master", "dall_e_9")
    assert images.provider_for("nano_banana_pro") == "fal" and images.job_vendor() == "fal"
    db.execute("update config set value = '\"openai\"'::jsonb where key = 'image_provider'")
    clear_config_cache()
    assert images.provider_for("gpt_image_flare") == "openai" and images.provider_for("nano_banana_pro") == "fal" and images.job_vendor() == "openai"


def test_fal_prices_depend_on_quality_edit_and_resolution(env):
    c = images.fal_image_cost
    assert c("gpt_image_flare", edit=True, quality="high") == 0.0495 and c("gpt_image_flare", edit=False, quality="low") == 0.0048
    assert c("gpt_image_flare", edit=True, quality="xhigh") == 0.0495                       # a quality fal does not list is priced as high
    assert c("nano_banana_pro", edit=True, resolution="4K") == 0.30 and c("nano_banana_pro", edit=False, resolution="1K") == 0.15


# ---------------------------------------------------------------- embeddings through fal

def test_embeddings_go_through_fal_in_batches_and_keep_1536_numbers(env, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    import fal_client

    calls = []

    def fake(endpoint, arguments=None, **kw):
        calls.append((endpoint, arguments))
        n = len(arguments["input"])
        return {"data": [{"index": i, "embedding": [0.1] * 1536} for i in range(n)], "usage": {"prompt_tokens": n * 10, "total_tokens": n * 10, "cost": 0.000001 * n}}

    monkeypatch.setattr(fal_client, "subscribe", fake)
    vecs, usage = images.embed([f"text {i}" for i in range(130)])
    assert len(vecs) == 130 and len(vecs[0]) == 1536 and [len(c[1]["input"]) for c in calls] == [64, 64, 2]
    assert calls[0][0] == "openrouter/router/openai/v1/embeddings" and calls[0][1]["model"] == "openai/text-embedding-3-small"
    assert usage["vendor"] == "fal" and usage["model"] == "fal:openai/text-embedding-3-small" and usage["tokens"] == 1300 and usage["cost_usd"] == pytest.approx(0.00013)

    monkeypatch.setattr(fal_client, "subscribe", lambda e, arguments=None, **k: {"data": [{"index": 0, "embedding": [0.1] * 1024}], "usage": {}})
    with pytest.raises(PermanentError, match="1536"):
        images.embed(["x"])
    monkeypatch.setattr(fal_client, "subscribe", lambda e, arguments=None, **k: {"data": [{"index": 0, "embedding": [0.1] * 1536}], "usage": {"total_tokens": 1000000}})
    assert images.embed(["x"])[1]["cost_usd"] == pytest.approx(0.02)             # no cost from fal: priced at the OpenAI rate


def test_a_default_deployment_needs_no_openai_account(env):
    from scripts import preflight

    assert preflight.openai_needed() is False and preflight.check_openai()[0] == preflight.SKIP
    db.execute("update config set value = '\"openai\"'::jsonb where key = 'embedding_provider'")
    clear_config_cache()
    assert preflight.openai_needed() is True


# ---------------------------------------------------------------- the Look gate: redo with the model of your choice

class Spy:
    def __init__(self, monkeypatch):
        self.calls = []
        real = images.generate_image

        def spy(prompt, references=None, quality=None, *, purpose="default", model=None):
            self.calls.append(types.SimpleNamespace(prompt=prompt, refs=len(references or []), quality=quality, purpose=purpose, model=model))
            return real(prompt, references, quality, purpose=purpose, model=model)

        monkeypatch.setattr(images, "generate_image", spy)


def at_look_gate(runner):
    c = client()
    sid = start(c, data={"language": "en"})
    to_gate(runner, c, sid, 3)
    return c, sid


def assets(sid, kind):
    return db.fetch_all("select id, character_id, kind, tags from character_assets where series_id = %s and kind = %s order by created_at", (sid, kind))


def test_the_look_page_offers_every_picture_and_both_models(runner):
    c, sid = at_look_gate(runner)
    page = c.get(f"/productions/{sid}/review/look").text
    assert 'name="image_model"' in page and "Nano Banana Pro" in page and "GPT Image 2.5 Flare" in page and "(default)" in page
    assert f'value="{assets(sid, "master")[0]["id"]}"' in page and 'value="set:' in page and "Redo the ticked pictures" in page


def test_a_pack_picture_is_redone_with_nano_banana_pro_the_note_and_the_rejected_picture(runner, monkeypatch):
    c, sid = at_look_gate(runner)
    spy = Spy(monkeypatch)
    pack = assets(sid, "turnaround")[0]
    old_key = db.fetch_one("select r2_key_clean from character_assets where id = %s", (pack["id"],))["r2_key_clean"]
    r = gate(c, sid, 3, decision="reject", target=str(pack["id"]), notes="make the jacket dark green", image_model="nano_banana_pro")
    assert r.status_code == 303
    drive(runner, sid, at_gate(3))
    assert len(spy.calls) == 1
    call = spy.calls[0]
    assert call.model == "nano_banana_pro" and call.purpose == "pack" and "make the jacket dark green" in call.prompt and look_handlers.PREV_SENTENCE in call.prompt
    assert call.refs == 2                                                   # the master portrait and the rejected picture
    redo = db.fetch_one("select detail from audit_log where series_id = %s and action = 'note:look_redo'", (sid,))["detail"]
    assert redo["model"] == "nano_banana_pro" and redo["prev"] == old_key and redo["text"] == "make the jacket dark green"
    assert db.fetch_one("select 1 from character_assets where id = %s", (pack["id"],)) is None          # replaced, not duplicated
    assert len(assets(sid, "turnaround")) >= 1


def test_without_a_model_choice_the_default_model_is_used_and_a_note_still_applies(runner, monkeypatch):
    c, sid = at_look_gate(runner)
    spy = Spy(monkeypatch)
    pack = assets(sid, "expression")[0]
    gate(c, sid, 3, decision="reject", target=str(pack["id"]), notes="softer expression", image_model="")
    drive(runner, sid, at_gate(3))
    assert spy.calls[0].model is None and images.model_for(spy.calls[0].purpose) == "gpt_image_flare" and "softer expression" in spy.calls[0].prompt


def test_a_rejected_master_is_made_again_first_then_its_sheets_and_the_line_up(runner, monkeypatch):
    c, sid = at_look_gate(runner)
    master = assets(sid, "master")[0]
    cid = master["character_id"]
    packs_before = db.fetch_one("select count(*) n from character_assets where character_id = %s and kind in ('turnaround','expression','wardrobe','pose')", (cid,))["n"]
    lineups = db.fetch_one("select count(*) n from character_assets where series_id = %s and kind = 'lineup'", (sid,))["n"]
    spy = Spy(monkeypatch)
    assert gate(c, sid, 3, decision="reject", target=str(master["id"]), notes="older, with a scar", image_model="nano_banana_pro").status_code == 303
    drive(runner, sid, at_gate(3))
    purposes = [x.purpose for x in spy.calls]
    assert purposes[0] == "master" and spy.calls[0].model == "nano_banana_pro" and "older, with a scar" in spy.calls[0].prompt and spy.calls[0].quality == "high"
    assert purposes.count("pack") == packs_before + lineups                      # that character's sheets and the cast line-up were drawn again from the new master
    assert db.fetch_one("select count(*) n from character_assets where series_id = %s and kind = 'master'", (sid,))["n"] == len({a["character_id"] for a in assets(sid, "master")})
    assert assets(sid, "master") and db.fetch_one("select count(*) n from character_assets where character_id = %s and kind = 'voice_sample'", (cid,))["n"] <= 1


def test_a_set_plate_can_be_redone_with_a_note(runner, monkeypatch):
    c, sid = at_look_gate(runner)
    plate = db.fetch_one("select id, plate_r2_key from sets where series_id = %s and plate_r2_key is not null order by code limit 1", (sid,))
    spy = Spy(monkeypatch)
    assert gate(c, sid, 3, decision="reject", target=f"set:{plate['id']}", notes="rain on the window", image_model="nano_banana_pro").status_code == 303
    drive(runner, sid, at_gate(3))
    assert len(spy.calls) == 1 and spy.calls[0].purpose == "plate" and spy.calls[0].model == "nano_banana_pro" and "rain on the window" in spy.calls[0].prompt
    assert db.fetch_one("select plate_r2_key from sets where id = %s", (plate["id"],))["plate_r2_key"]


def test_a_swapped_voice_still_works_and_bad_choices_are_refused(runner):
    c, sid = at_look_gate(runner)
    voice = db.fetch_one("select id from character_assets where series_id = %s and kind = 'voice_sample' limit 1", (sid,))
    if voice:
        assert gate(c, sid, 3, decision="reject", target=str(voice["id"]), notes="").status_code == 303
        drive(runner, sid, at_gate(3))
        assert db.fetch_one("select 1 from character_assets where series_id = %s and kind = 'voice_sample'", (sid,))
        pack = assets(sid, "turnaround")[0]
    else:
        pack = assets(sid, "turnaround")[0]
    assert gate(c, sid, 3, decision="reject", target=str(pack["id"]), image_model="dall_e_9").status_code == 400
    assert gate(c, sid, 3, decision="reject", target="", image_model="nano_banana_pro").status_code == 400
    assert gate(c, sid, 3, decision="reject", target="not-an-id").status_code == 400


# ---------------------------------------------------------------- ad shots default to Nano Banana Pro

def test_ad_placement_keyframes_use_nano_banana_pro_and_other_keyframes_the_default(runner, monkeypatch):
    c = client()
    sid = ad_production(c)
    spy = Spy(monkeypatch)
    to_gate(runner, c, sid, 4)
    kf = [x for x in spy.calls if x.purpose in ("keyframe", "brand_keyframe")]
    assert {x.purpose for x in kf} == {"keyframe", "brand_keyframe"}
    assert images.model_for("brand_keyframe") == "nano_banana_pro" and images.model_for("keyframe") == "gpt_image_flare"
    assert all(x.refs >= 1 for x in kf)


# ---------------------------------------------------------------- Gate 4/5 feedback reaches the models

def test_picture_feedback_text_and_the_rejected_frame_reach_the_keyframe_and_the_shot_request(runner, monkeypatch):
    c = client()
    sid = start(c, data={"language": "en"})
    to_gate(runner, c, sid, 4)
    ep = db.fetch_one("select id, number from episodes where series_id = %s and batch = 'pilot' order by number limit 1", (sid,))
    with db.transaction() as conn:
        item = feedback.create_item(conn, sid, user_id(), f"Episode {ep['number']} (00:00-00:03)", "her eyes look closed in the first second")
        feedback.confirm_item(conn, sid, item["id"], user_id(), "picture")
    shot_ids = db.fetch_one("select shot_ids from feedback_items where id = %s", (item["id"],))["shot_ids"]
    prev = {s_: db.fetch_one("select keyframe_r2_key from shots where id = %s", (s_,))["keyframe_r2_key"] for s_ in shot_ids}
    spy = Spy(monkeypatch)
    seen = []
    real = llm.complete

    def llm_spy(role, system, messages, *a, **k):
        seen.append((role, " ".join(str(m["content"]) for m in messages)))
        return real(role, system, messages, *a, **k)

    monkeypatch.setattr(llm, "complete", llm_spy)
    with db.transaction() as conn:
        decisions.decide_feedback(conn, sid, user_id())
    drive(runner, sid, at_gate(4))
    for s_ in shot_ids:
        spec = db.fetch_one("select spec from shots where id = %s", (s_,))["spec"]
        assert spec["feedback_note"] == "her eyes look closed in the first second" and spec["prev_keyframe"] == prev[s_]
    redone = [x for x in spy.calls if "her eyes look closed in the first second" in x.prompt]
    assert len(redone) >= len(shot_ids) and all("previous version of this frame" in x.prompt for x in redone)
    assert any(role == "compiler" and "Reviewer feedback on the previous version of this shot (apply it): her eyes look closed" in text for role, text in seen)
