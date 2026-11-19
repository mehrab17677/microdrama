"""Phase 11: ad placement. Brand assets are reproduced exactly (full-frame phone and product inserts), checked by pixels, rebuilt when wrong, and reported."""
import io
import json
import subprocess
import uuid
from pathlib import Path

import numpy as np
import pytest
from helpers import at_gate, drive
from PIL import Image, ImageDraw
from test_phase5 import brief, client, env, gate, jobs_of, post_json, runner, start, to_gate  # noqa: F401 (fixtures)
from test_phase6 import job_for

from app import db, placement
from app.jobs.handlers import qa_handlers as Q
from app.media import durations, ffmpeg
from app.qa import repair
from app.stages import brand_qa, shots as S
from app.vendors import llm, storage

TIMING = {"pad_head": 0.25, "pad_tail": 0.35, "line_gap": 0.30}


# ---------------------------------------------------------------- assets that look like real screens (not flat colour)

def screen(seed: int, size=(540, 1170)) -> bytes:
    """A made-up app screen: gradient, header bar, rows and text-like strokes, so a distorted copy cannot match it by accident."""
    rng = np.random.default_rng(seed)
    w, h = size
    img = Image.new("RGB", size)
    px = np.zeros((h, w, 3), dtype=np.uint8)
    px[:] = (rng.integers(40, 200), rng.integers(40, 200), rng.integers(40, 200))
    px += (np.linspace(0, 55, h)[:, None, None]).astype(np.uint8)
    img = Image.fromarray(px)
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, w, 90), fill=(20, 20, 30))
    for i in range(9):
        y = 140 + i * 100
        d.rounded_rectangle((30, y, w - 30, y + 70), 12, fill=(250, 250, 250))
        d.line((50, y + 20, 50 + int(rng.integers(100, 300)), y + 20), fill=(30, 30, 30), width=6)
        d.line((50, y + 45, 50 + int(rng.integers(60, 200)), y + 45), fill=(120, 120, 120), width=4)
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def ad_production(c, kinds=("phone_full", "product_full"), color="#1A2B3C", wording="", exclusions="scams", screens=2):
    data, files = brief(data={"category": "ad_placement", "brand_name": "Acme", "product": "Pay app", "placement_types": list(kinds), "frequency_note": "every 3rd episode",
                              "exclusions": exclusions, "spoken_wording": wording, "brand_color": color,
                              "brand_kinds": ["app_screen"] * screens + ["product", "logo"], "brand_labels": [f"Screen {i + 1}" for i in range(screens)] + ["Can", "Logo"]})
    for i in range(screens):
        files.append(("brand_files", (f"s{i}.png", screen(i), "image/png")))
    files += [("brand_files", ("can.png", screen(50, (600, 800)), "image/png")), ("brand_files", ("logo.png", screen(51, (300, 300)), "image/png"))]
    r = post_json(c, data, files)
    assert r.status_code == 303, r.text
    return uuid.UUID(r.headers["location"].rsplit("/", 1)[1])


# ---------------------------------------------------------------- intake

def test_an_ad_production_cannot_start_without_a_matching_readable_brand_asset(env):
    c = client()
    base = {"category": "ad_placement", "brand_name": "Acme", "product": "Pay app", "frequency_note": "x"}
    d, f = brief(data={**base, "placement_types": ["phone_full"]})
    assert "at least one brand asset" in " | ".join(post_json(c, d, f).json()["errors"])
    d, f = brief(data={**base, "placement_types": ["phone_full"], "brand_kinds": ["logo"]})
    f.append(("brand_files", ("logo.png", screen(1), "image/png")))
    assert "phone_full placement needs at least one app screen asset" in " | ".join(post_json(c, d, f).json()["errors"])      # a logo cannot stand in for the screens
    d, f = brief(data={**base, "placement_types": ["product_full"], "brand_kinds": ["app_screen"]})
    f.append(("brand_files", ("s.png", screen(1), "image/png")))
    assert "product_full placement needs at least one product image asset" in " | ".join(post_json(c, d, f).json()["errors"])
    d, f = brief(data={**base, "placement_types": ["phone_full"], "brand_kinds": ["app_screen"]})
    f.append(("brand_files", ("s.png", b"not an image", "image/png")))
    assert "is not a readable image" in " | ".join(post_json(c, d, f).json()["errors"])
    d, f = brief(data={**base, "placement_types": ["phone_full"], "brand_kinds": ["app_screen"]})
    f.append(("brand_files", ("tiny.png", screen(1, (100, 100)), "image/png")))
    assert "too small" in " | ".join(post_json(c, d, f).json()["errors"])
    d, f = brief(data={**base, "placement_types": ["phone_full"], "brand_kinds": ["app_screen"], "brand_color": "blue"})
    f.append(("brand_files", ("s.png", screen(1), "image/png")))
    assert "brand colour must look like #1A2B3C" in " | ".join(post_json(c, d, f).json()["errors"])
    sid = ad_production(c, color="#1a2b3c")
    assert db.fetch_one("select brand_color from brand_briefs where series_id = %s", (sid,))["brand_color"] == "#1A2B3C"
    assert db.fetch_one("select count(*) n from brand_assets where series_id = %s", (sid,))["n"] == 4


# ---------------------------------------------------------------- composing and building (real ffmpeg, pixel checks)

def test_a_screen_is_scaled_to_fit_never_cropped_or_stretched_on_the_brand_colour():
    asset = screen(3)                                                                   # 540x1170: a phone-shaped screen
    frame = placement.compose_frame(asset, "#1A2B3C")
    assert frame.size == (1080, 1920) and frame.getpixel((3, 3)) == (0x1A, 0x2B, 0x3C) and frame.getpixel((1076, 1916)) == (0x1A, 0x2B, 0x3C)
    scale = min(1080 / 540, 1920 / 1170)
    w, h = round(540 * scale), round(1170 * scale)
    box = ((1080 - w) // 2, (1920 - h) // 2, (1080 - w) // 2 + w, (1920 - h) // 2 + h)
    assert abs(frame.crop(box).size[0] - w) <= 1 and frame.getpixel((box[0] - 4, 900)) == (0x1A, 0x2B, 0x3C)               # pillarbox bars
    expected = Image.open(io.BytesIO(asset)).convert("RGB").resize((w, h), Image.LANCZOS)
    diff = np.abs(np.asarray(frame.crop(box), dtype=int) - np.asarray(expected, dtype=int)).mean()
    assert diff < 2.0                                                                    # the asset itself is unchanged
    wide = placement.compose_frame(screen(4, (1600, 400)), "#FFFFFF")                    # a wide image gets bars above and below
    assert wide.getpixel((540, 5)) == (255, 255, 255)
    transparent = io.BytesIO()
    Image.new("RGBA", (400, 400), (255, 0, 0, 0)).save(transparent, "PNG")             # a fully transparent logo shows the brand colour, not black
    assert placement.compose_frame(transparent.getvalue(), "#00FF00").getpixel((540, 960)) == (0, 255, 0)


def test_holds_add_up_to_the_planned_seconds():
    for planned, n in ((4.0, 3), (6.0, 2), (3.3, 4), (10, 1)):
        h = placement.holds(planned, n)
        assert len(h) == n and sum(h) == pytest.approx(planned, abs=1e-6)


def test_a_built_insert_shows_each_supplied_screen_unchanged_with_hard_cuts(tmp_path, monkeypatch):
    monkeypatch.setenv("FFMPEG_PRESET", "ultrafast")
    assets = [screen(1), screen(2), screen(3)]
    out = tmp_path / "insert.mp4"
    placement.build_insert(assets, "#1A2B3C", 6.0, out, tmp_path)
    info = ffmpeg.probe(out)
    assert (info["width"], info["height"], info["fps"]) == (1080, 1920, 24.0) and info["duration"] == pytest.approx(6.0, abs=0.1) and not info["has_audio"]
    ok, worst, detail = placement.verify_insert(out, assets, "#1A2B3C", 6.0)
    assert ok and worst < 6.0 and detail["screens"] == 3
    # pixel comparison of a frame against the supplied screen, in the region the asset occupies
    frame = placement.frame_at(out, 1.0, tmp_path / "f.png")                             # inside the first hold (0 to 2 s)
    expected = placement.compose_frame(assets[0], "#1A2B3C")
    assert placement.mean_abs_diff(frame, expected) < 4.0
    assert placement.mean_abs_diff(frame, placement.compose_frame(assets[1], "#1A2B3C")) > 15       # and it is NOT the second screen
    assert placement.mean_abs_diff(placement.frame_at(out, 3.0, tmp_path / "g.png"), placement.compose_frame(assets[1], "#1A2B3C")) < 4.0   # hard cut to screen two


def test_a_distorted_or_wrong_insert_fails_the_pixel_check(tmp_path, monkeypatch):
    monkeypatch.setenv("FFMPEG_PRESET", "ultrafast")
    assets = [screen(1), screen(2)]
    wrong = tmp_path / "wrong.mp4"
    placement.build_insert(assets, "#FF0000", 4.0, wrong, tmp_path)                       # the wrong brand colour
    assert placement.verify_insert(wrong, assets, "#1A2B3C", 4.0)[0] is False
    swapped = tmp_path / "swapped.mp4"
    placement.build_insert(list(reversed(assets)), "#1A2B3C", 4.0, swapped, tmp_path)     # the screens in the wrong order
    assert placement.verify_insert(swapped, assets, "#1A2B3C", 4.0)[0] is False
    stretched = tmp_path / "stretched.png"
    Image.open(io.BytesIO(assets[0])).convert("RGB").resize((1080, 1920)).save(stretched)  # restyled: stretched to the full frame
    assert placement.mean_abs_diff(Image.open(stretched).convert("RGB"), placement.compose_frame(assets[0], "#1A2B3C")) > 6.0


# ---------------------------------------------------------------- the shot list carries the placement

def ad_episode(sid, number):
    return db.fetch_one("select * from episodes where series_id = %s and number = %s", (sid, number))


@pytest.fixture()
def ad_series(env):
    from test_phase7 import look_ready

    c = client()
    sid = ad_production(c)
    look_ready(sid)
    db.execute("delete from placements where series_id = %s", (sid,))
    assets = {k: db.fetch_one("select id from brand_assets where series_id = %s and kind = %s order by created_at limit 1", (sid, k))["id"] for k in ("app_screen", "product")}
    db.execute("insert into placements (series_id, episode_number, kind, description, brand_asset_id) values (%s, 1, 'phone_full', 'She pays with the app.', %s)", (sid, assets["app_screen"]))
    return sid


def test_the_director_must_add_the_planned_brand_insert_and_it_is_linked_to_its_placement(ad_series):
    from app.jobs.handlers import build_handlers as B

    sid = ad_series
    ep = ad_episode(sid, 1)
    good = S.fixture_shot_list(sid, ep)
    inserts = [s for s in good.shots if s.placement_kind == "phone_full"]
    assert len(inserts) == 1 and inserts[0].shot_type == "static_insert" and not inserts[0].characters and not inserts[0].line_indexes and 8 <= len(good.shots) <= 12
    assert S.validate_shot_list(sid, ep, good.shots, {"Static medium"}) == []
    material = S.build_material(sid, ep, {"language": "bn"}, [])
    assert "Brand placement planned for this episode" in material and "phone_full (2 image(s) to show, so est_seconds of at least 2)" in material and "She pays with the app." in material
    # a missing insert, an unplanned kind, an insert with an actor and one that is too short for its images each fail
    without = [s.model_copy(update={"placement_kind": None}) for s in good.shots]
    assert any("placement is missing" in p for p in S.validate_shot_list(sid, ep, without, {"Static medium"}))
    i = next(i for i, s in enumerate(good.shots) if s.placement_kind)
    odd = [s.model_copy() for s in good.shots]
    odd[i] = odd[i].model_copy(update={"placement_kind": "signage"})
    assert any("not in this episode's placement plan" in p for p in S.validate_shot_list(sid, ep, odd, {"Static medium"}))
    actor = [s.model_copy() for s in good.shots]
    actor[i] = actor[i].model_copy(update={"characters": good.shots[0].characters})
    assert any("static_insert with no characters and no lines" in p for p in S.validate_shot_list(sid, ep, actor, {"Static medium"}))
    short = [s.model_copy() for s in good.shots]
    short[i] = short[i].model_copy(update={"est_seconds": 1.0})
    assert any("est_seconds must be between 2 and 10" in p for p in S.validate_shot_list(sid, ep, short, {"Static medium"}))
    db.execute("insert into brand_assets (series_id, kind, label, r2_key) select series_id, 'app_screen', 'Extra', r2_key from brand_assets where series_id = %s and kind = 'app_screen' limit 1", (sid,))
    db.execute("insert into brand_assets (series_id, kind, label, r2_key) select series_id, 'app_screen', 'Extra 2', r2_key from brand_assets where series_id = %s and kind = 'app_screen' limit 1", (sid,))
    tight = [s.model_copy() for s in good.shots]
    tight[i] = tight[i].model_copy(update={"est_seconds": 2.0})
    assert any("4 image(s) need at least 3.2 seconds" in p for p in S.validate_shot_list(sid, ep, tight, {"Static medium"}))
    B.llm_shot_list(job_for(sid, "llm_shot_list", ep["id"], episode=1))
    row = db.fetch_one("select p.status, s.idx, s.tier, s.shot_type from placements p join shots s on s.id = p.shot_id where p.series_id = %s", (sid,))
    assert row["tier"] == "INSERT" and row["shot_type"] == "static_insert" and row["status"] == "planned"


def test_a_brand_insert_keeps_its_seconds_when_the_episode_is_scaled_to_the_runtime():
    shots = [{"idx": 0, "tier": "A", "line_durations": [], "est_seconds": 3.0}, {"idx": 1, "tier": "INSERT", "line_durations": [], "est_seconds": 4.0, "fixed": True},
             {"idx": 2, "tier": "B2", "line_durations": [20.0], "est_seconds": None}] + [{"idx": 3 + i, "tier": "A", "line_durations": [], "est_seconds": 5.0} for i in range(5)]
    fit = durations.fit_episode(shots, TIMING)
    assert fit["planned"][1] == 4.0 and 75 <= fit["runtime"] <= 90                       # the other silent shots absorbed the scaling


# ---------------------------------------------------------------- the whole production, in the graph

@pytest.fixture()
def ad_pilot(runner):
    c = client()
    sid = ad_production(c, color="#1A2B3C")
    to_gate(runner, c, sid, 4)
    return c, sid


def test_an_ad_drama_places_the_phone_insert_checks_it_and_reports_it(ad_pilot):
    c, sid = ad_pilot
    p = db.fetch_one("select p.*, s.idx, s.tier, s.status shot_status, s.planned_duration, s.final_clip_r2_key, s.spec from placements p join shots s on s.id = p.shot_id where p.series_id = %s and p.episode_number = 2", (sid,))
    assert p["kind"] == "phone_full" and p["tier"] == "INSERT" and p["status"] == "verified" and p["shot_status"] == "qa_passed" and p["spec"]["placement_kind"] == "phone_full"
    assert float(p["planned_duration"]) >= 4.0                                           # two screens, at least two seconds each
    clip = storage.local_file(p["final_clip_r2_key"])
    info = ffmpeg.probe(clip)
    assert (info["width"], info["height"]) == (1080, 1920) and info["duration"] == pytest.approx(float(p["planned_duration"]), abs=0.15)
    assets = []
    for r in placement.asset_rows(sid, "app_screen"):
        f = Path(storage.local_file(r["r2_key"]))
        assets.append(f.read_bytes())
    assert placement.verify_insert(clip, assets, "#1A2B3C", float(p["planned_duration"]))[0]                    # the supplied screens, unchanged
    rows = {r["check_name"]: r for r in db.fetch_all("select * from qa_results where episode_id = (select id from episodes where series_id = %s and number = 2) and check_name in ('placement_count','exclusions') and shot_id is null", (sid,))}
    assert rows["placement_count"]["passed"] and rows["exclusions"]["passed"] and rows["placement_count"]["detail"]["in_cut"] == 1
    assert db.fetch_one("select passed from qa_results where shot_id = (select shot_id from placements where series_id = %s and episode_number = 2) and check_name = 'asset_intact'", (sid,))["passed"]
    # no video model for the insert itself:
    assert db.fetch_one("select count(*) n from jobs where shot_id = %s and kind in ('video_clip','lipsync','image_keyframe')", (p["shot_id"],))["n"] == 0
    # the report: episode, timestamp from the latest cut
    rep = [r for r in placement.report(sid) if r["episode"] == 2][0]
    shots = db.fetch_all("select e.start_seconds, e.end_seconds from edit_entries e join shots s on s.id = e.shot_id where s.id = %s and e.cut_version = (select cut_version from episodes where series_id = %s and number = 2)", (p["shot_id"], sid))
    assert rep["status"] == "verified" and rep["shot_idx"] == p["idx"] and rep["start"] == pytest.approx(float(shots[0]["start_seconds"])) and rep["end"] == pytest.approx(float(shots[0]["end_seconds"]))
    assert rep["time"] == f"{placement.mmss(rep['start'])}-{placement.mmss(rep['end'])}" and rep["assets"] == ["Screen 1", "Screen 2"]
    ep5 = [r for r in placement.report(sid) if r["episode"] == 5][0]
    assert ep5["kind"] == "product_full" and ep5["status"] == "planned" and ep5["time"] == "" and ep5["assets"] == ["Can"]                # later episodes are reported as planned
    page = c.get(f"/productions/{sid}/review/episodes").text
    assert "Placement report" in page and 'data-kind="phone_full"' in page and f"/productions/{sid}/placements.csv" in page
    csv_text = c.get(f"/productions/{sid}/placements.csv")
    assert csv_text.status_code == 200 and csv_text.text.splitlines()[0].startswith("episode,placement,method,status") and "phone_full" in csv_text.text and "Screen 1; Screen 2" in csv_text.text
    assert client("other@x.io").get(f"/productions/{sid}/placements.csv").status_code == 200                            # anybody signed in can view it
    assert csv_text.headers["content-disposition"].startswith("attachment")
    assert "Placement report" not in c.get(f"/productions/{start(client())}/review/episodes").text                      # a standard drama has none


def test_a_placement_in_an_excluded_scene_fails_the_exclusion_check(ad_pilot, monkeypatch):
    c, sid = ad_pilot
    ep = ad_episode(sid, 2)
    real = llm.complete

    def complete(role, system, messages, schema=None, images=None, fixture=None, cache_system=False):
        if schema is brand_qa.ExclusionCheck:
            fixture = lambda: brand_qa.ExclusionCheck(violations=[brand_qa.ExclusionViolation(exclusion="scams", reason="the scene shows a scam")])
        return real(role, system, messages, schema, images, fixture, cache_system)

    monkeypatch.setattr(llm, "complete", complete)
    job = {"id": uuid.uuid4(), "series_id": sid, "episode_id": ep["id"], "kind": "qa_brand", "request": {"round": "qa2"}}
    out = Q.qa_brand(job)
    assert out["failed"] == ["exclusions"]
    row = db.fetch_one("select passed, detail from qa_results where episode_id = %s and check_name = 'exclusions' and qa_round = 'qa2'", (ep["id"],))
    assert row["passed"] is False and row["detail"]["violations"][0]["exclusion"] == "scams" and row["detail"]["violations"][0]["placement"] == "phone_full"
    assert db.fetch_one("select passed from qa_results where episode_id = %s and check_name = 'placement_count' and qa_round = 'qa2'", (ep["id"],))["passed"]
    placement.finalize(sid, "pilot")                                                      # the newest episode checks are the failing ones
    assert db.fetch_one("select status from placements where series_id = %s and episode_number = 2", (sid,))["status"] == "failed"


def test_the_exclusion_check_reads_the_placement_scene_and_the_list_and_skips_when_there_is_nothing_to_exclude(ad_pilot, monkeypatch):
    c, sid = ad_pilot
    ep = ad_episode(sid, 2)
    seen = []
    real = llm.complete
    monkeypatch.setattr(llm, "complete", lambda role, system, messages, **k: (seen.append((role, messages[0]["content"])), real(role, system, messages, **k))[1])
    rows, usage = brand_qa.evaluate(sid, ep)
    role, text = seen[0]
    assert role == "writer" and "Exclusion list: scams" in text and "Brand placement in this scene: phone_full" in text and "Scene " in text and dict((c, p) for c, p, _ in rows) == {"placement_count": True, "exclusions": True}
    db.execute("update brand_briefs set exclusions = '{}' where series_id = %s", (sid,))
    seen.clear()
    assert brand_qa.evaluate(sid, ep)[1] is None and seen == []                           # nothing to check, no model call
    assert brand_qa.evaluate(sid, ad_episode(sid, 1))[0] == [("placement_count", True, {"planned": 0}), ("exclusions", True, {"planned": 0})]   # an episode with no placement


def test_an_insert_that_fails_the_pixel_check_is_rebuilt_not_shipped(runner, monkeypatch):
    calls = []
    real = placement.build_insert

    def sabotaged(assets, color, planned, out, workdir):
        calls.append(color)
        return real(assets, "#FF0000" if len(calls) == 1 else color, planned, out, workdir)        # the first build uses the wrong colour

    monkeypatch.setattr(placement, "build_insert", sabotaged)
    c = client()
    sid = ad_production(c)
    to_gate(runner, c, sid, 4)
    p = db.fetch_one("select p.status, s.attempts, s.status shot_status, s.id from placements p join shots s on s.id = p.shot_id where p.series_id = %s and p.episode_number = 2", (sid,))
    assert len(calls) == 2 and p["attempts"] == 1 and p["shot_status"] == "qa_passed" and p["status"] == "verified"           # rebuilt once, then it matched
    rows = db.fetch_all("select passed, detail from qa_results where shot_id = %s and check_name = 'asset_intact' order by created_at", (p["id"],))
    assert [r["passed"] for r in rows] == [False, True] and [r["detail"]["attempts"] for r in rows] == [0, 1]
    assert db.fetch_one("select count(*) n from jobs where shot_id = %s and kind = 'ffmpeg_insert'", (p["id"],))["n"] == 2
    assert db.fetch_one("select count(*) n from jobs where shot_id = %s and kind in ('video_clip','image_keyframe')", (p["id"],))["n"] == 0     # no vendor call for the repair
    assert db.fetch_one("select count(*) n from audit_log where series_id = %s and action like 'note:repair:2:%%'", (sid,))["n"] == 1


def test_the_repair_table_rebuilds_a_failed_asset_check_and_gives_up_after_three():
    shot = {"id": uuid.uuid4(), "tier": "INSERT", "line_count": 0, "fallback_used": False, "sync_attempts": 0, "attempts": 0, "planned_duration": 4, "spec": {}}
    p = repair.decide(shot, [("asset_intact", "asset")])
    assert p["action"] == "rebuild" and p["set"] == {"status": "keyframe_ready", "attempts": 1} and p["reset"]
    assert repair.decide({**shot, "attempts": 3}, [("asset_intact", "asset")])["action"] == "needs_human"


# ---------------------------------------------------------------- the other placement types

def test_a_composited_placement_is_planned_and_reported_but_never_silently_passed(runner):
    c = client()
    sid = ad_production(c, kinds=("phone_in_hand",))
    to_gate(runner, c, sid, 4)
    shot = db.fetch_one("select s.* from placements p join shots s on s.id = p.shot_id where p.series_id = %s and p.episode_number = 2", (sid,))
    assert shot["spec"]["placement_kind"] == "phone_in_hand" and shot["tier"] == "A" and shot["status"] == "qa_passed"
    row = db.fetch_one("select passed, category, detail from qa_results where shot_id = %s and check_name = 'asset_intact'", (shot["id"],))
    assert row["passed"] is False and row["category"] == "unscored" and row["detail"]["manual_review"] is True
    rep = [r for r in placement.report(sid) if r["episode"] == 2][0]
    assert rep["kind"] == "phone_in_hand" and rep["status"] == "planned" and "not built yet" in rep["method"] and rep["time"]       # a person judges it at the gate
    assert "phone_in_hand" in c.get(f"/productions/{sid}/review/episodes").text and "not built yet" in c.get(f"/productions/{sid}/review/episodes").text


def test_a_spoken_mention_must_actually_be_said(env):
    sid = start(client())
    ep = db.fetch_one("insert into episodes (series_id, number, batch, status, beat, cut_version, final_r2_key) values (%s, 3, 'pilot', 'compiled', '{}', 1, 'x') returning *", (sid,))
    ch = db.fetch_one("insert into characters (series_id, code, name, role, is_lead, visual_description) values (%s, 'CH01', 'A', 'lead', true, 'x') returning id", (sid,))["id"]
    db.execute("insert into dialogue_lines (series_id, episode_id, idx, character_id, text, emotion) values (%s,%s,0,%s,'I always pay with Acme Pay.','calm')", (sid, ep["id"], ch))
    shot = db.fetch_one("insert into shots (series_id, episode_id, idx, shot_type, tier, spec, status) values (%s,%s,0,'speaking_wider','B2',%s,'qa_passed') returning id", (sid, ep["id"], json.dumps({"placement_kind": "spoken", "scene_index": 0})))
    db.execute("insert into edit_entries (episode_id, cut_version, shot_id, start_seconds, end_seconds) values (%s,1,%s,0,4)", (ep["id"], shot["id"]))
    db.execute("insert into placements (series_id, episode_number, kind, description, shot_id, status) values (%s,3,'spoken','She says the name.',%s,'planned')", (sid, shot["id"]))
    db.execute("insert into brand_briefs (series_id, version, brand_name, product, placement_types, frequency_note, exclusions, spoken_wording) values (%s,1,'Acme','Pay','{spoken}','x','{}',%s)", (sid, "use acme pay"))
    rows, _ = brand_qa.evaluate(sid, db.fetch_one("select * from episodes where id = %s", (ep["id"],)))
    assert dict((c, p) for c, p, _ in rows)["placement_count"] is False and "wording is not said" in rows[0][2]["missing"][0]["why"]
    db.execute("update dialogue_lines set text = 'Please use Acme Pay today.' where episode_id = %s", (ep["id"],))
    rows, _ = brand_qa.evaluate(sid, db.fetch_one("select * from episodes where id = %s", (ep["id"],)))
    assert dict((c, p) for c, p, _ in rows)["placement_count"] is True                     # the exact wording, in a scene the brand allows


def test_the_product_insert_uses_the_product_images_on_the_brand_colour(runner):
    c = client()
    sid = ad_production(c, kinds=("product_full",), color="#00AA55")
    to_gate(runner, c, sid, 4)
    p = db.fetch_one("select s.final_clip_r2_key, s.planned_duration from placements p join shots s on s.id = p.shot_id where p.series_id = %s and p.episode_number = 2", (sid,))
    clip = storage.local_file(p["final_clip_r2_key"])
    frame = placement.frame_at(clip, 0.5, Path(clip).parent / "f.png")
    assert frame.getpixel((2, 2)) == pytest.approx((0, 0xAA, 0x55), abs=3)                # the brand colour around the can
    can = Path(storage.local_file(placement.asset_rows(sid, "product")[0]["r2_key"])).read_bytes()
    assert placement.mean_abs_diff(frame, placement.compose_frame(can, "#00AA55")) < 4.0
    assert [r["assets"] for r in placement.report(sid) if r["episode"] == 2] == [["Can"]]
