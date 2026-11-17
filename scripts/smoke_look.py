"""Real-model smoke test of the look stage (phase 7).

    DRY_RUN=0 IMAGE_BACKEND=local_sd VOICE_BACKEND=local EMBEDDING_BACKEND=local \
        python scripts/smoke_look.py --out DIR --photo PATH_TO_A_FACE_PHOTO

Real Claude (vision, voice profiles) and gpt-6-luna (emotion groups); the open-source image and voice backends stand in for
OpenAI images and ElevenLabs (see docs/OPEN_SOURCE_TEST_SUBSTITUTES.md). English series only (the local voice is English-only).
"""
from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from psycopg.types.json import Jsonb  # noqa: E402

from app import db  # noqa: E402
from app.jobs.handlers import look_handlers as L  # noqa: E402
from app.qa import face  # noqa: E402
from app.stages import look, story, story_fixtures as fx  # noqa: E402
from app.vendors import storage  # noqa: E402
from app.web import auth  # noqa: E402

LINES = [("CH01", "You knew all along, and you let me walk into this.", "angry"), ("CH01", "I trusted you with everything I had left.", "sad"),
         ("CH02", "Trust is a luxury people like us cannot afford.", "cold"), ("CH02", "Leave now, before someone sees you here.", "urgent"),
         ("CH01", "No. Not until you tell me the truth.", "defiant"), ("CH01", "Say it. Say what really happened that night.", "angry"),
         ("CH02", "It was never about the money.", "weary"), ("CH02", "I was protecting you from yourself.", "tender")]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--photo", required=True)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    tag = uuid.uuid4().hex[:6]
    cost: dict[str, float] = {}
    clock: dict[str, float] = {}

    def step(name, fn):
        t = time.time()
        r = fn()
        clock[name], cost[name] = time.time() - t, (r or {}).get("actual_cost", 0.0) if isinstance(r, dict) else 0.0
        print(f"{name}: {clock[name]:.0f}s ${cost[name]:.4f}")
        return r

    owner = db.fetch_one("insert into users (email, display_name, password_hash, role, status) values (%s,'Smoke',%s,'member','active') returning id",
                         (f"look-{tag}@example.com", auth.hash_password("x" * 12)))["id"]
    sid = uuid.uuid4()
    db.execute("insert into series (id, owner_id, title, category, language, custom_genre, status, current_stage, thread_id) values (%s,%s,'Smoke look','standard','en','Drama','running','visual',%s)", (sid, owner, str(sid)))
    db.execute("insert into briefs (series_id, version, keywords, aesthetic_note) values (%s,1,%s,%s)",
               (sid, ["revenge", "village", "slow burn"], "warm amber light, muted teal palette, 1990s wardrobe, soft film grain"))
    photo = Path(args.photo).read_bytes()
    key = f"series/{sid}/brief/{uuid.uuid4()}.png"
    storage.put_bytes(key, photo)
    ref_id = db.fetch_one("insert into brief_refs (series_id, kind, role_label, r2_key, face_vector) values (%s,'character','female lead',%s,%s::vector) returning id",
                          (sid, key, db.to_vector(face.face_vector(photo))))["id"]
    story.persist_bible(sid, fx.make_bible({"roles": ["female lead"], "language": "en", "bible": None}))
    chars = {c["code"]: c for c in db.fetch_all("select * from characters where series_id = %s order by code", (sid,))}
    ep = db.fetch_one("insert into episodes (series_id, number, batch, status, beat) values (%s,1,'pilot','scripted','{}'::jsonb) returning id", (sid,))["id"]
    for i, (code, text, emotion) in enumerate(LINES):
        db.execute("insert into dialogue_lines (series_id, episode_id, idx, character_id, text, emotion) values (%s,%s,%s,%s,%s,%s)", (sid, ep, i, chars[code]["id"], text, emotion))
    for c in chars.values():
        db.execute("insert into wardrobe_items (series_id, character_id, code, description) values (%s,%s,'W01','Dark wool coat over a cream blouse.')", (sid, c["id"]))

    def job(job_kind, **req):
        return {"id": uuid.uuid4(), "kind": job_kind, "series_id": sid, "episode_id": None, "request": {"rev": 0, **req}}

    ts = step("trait sheet (Claude vision)", lambda: L.llm_trait_sheet(job("llm_trait_sheet", ref_id=str(ref_id))))
    print("   ->", ts["trait_sheet"])
    for code, c in chars.items():
        step(f"voice profile {code}", lambda c=c: L.llm_voice_profile(job("llm_voice_profile", character_id=str(c["id"]))))
        step(f"emotion groups {code} (gpt-6-luna)", lambda c=c: L.llm_audio_tag(job("llm_audio_tag", purpose="emotion_groups", character_id=str(c["id"]))))
        print("   ->", db.fetch_one("select voice_profile->>'design_prompt' d, emotion_groups from characters where id = %s", (c["id"],)))
    # the graph reads the trait sheet from the job record; do the same here
    db.execute("insert into jobs (series_id, kind, idempotency_key, request, status, result, finished_at) values (%s,'llm_trait_sheet',%s,%s::jsonb,'succeeded',%s,now())",
               (sid, uuid.uuid4().hex, f'{{"ref_id": "{ref_id}"}}', Jsonb(ts)))
    for code, c in chars.items():
        step(f"master {code}", lambda c=c: L.image_master(job("image_master", character_id=str(c["id"]), **{"try": 1}, use_photo=True)))
    master = db.fetch_one("select id, r2_key_clean, r2_key_review, face_vector::text fv from character_assets where character_id = %s and kind = 'master'", (chars["CH01"]["id"],))
    ref_vec = face.face_vector(photo)
    sim = face.similarity([float(x) for x in master["fv"].strip("[]").split(",")], ref_vec)
    print(f"   likeness: master vs your photo = {sim:.2f} (must be under 0.30 to pass; over it the graph regenerates, the third try without the photo)")
    for kind, tags in (("turnaround", {"angle": "front"}), ("expression", {"emotion_group": "anger"}), ("wardrobe", {"outfit": "W01", "view": "full_body_front"})):
        step(f"pack {kind}", lambda kind=kind, tags=tags: L.image_pack(job("image_pack", character_id=str(chars["CH01"]["id"]), kind=kind, tags=tags, **{"try": 0})))
    rows = db.fetch_all("select kind, face_vector::text fv, r2_key_review from character_assets where character_id = %s and kind <> 'master'", (chars["CH01"]["id"],))
    mv = [float(x) for x in master["fv"].strip("[]").split(",")]
    for r in rows:
        v = r["fv"] and [float(x) for x in r["fv"].strip("[]").split(",")]
        print(f"   pack {r['kind']}: match to master = {face.similarity(v, mv):.2f} (needs 0.40)" if v else f"   pack {r['kind']}: no face detected (counts as pass)")
    step("lineup", lambda: L.image_pack(job("image_pack", character_id=None, kind="lineup", tags={}, **{"try": 0})))
    s1 = db.fetch_one("select id from sets where series_id = %s order by code limit 1", (sid,))
    step("set plate", lambda: L.image_set_plate(job("image_set_plate", set_id=str(s1["id"]))))
    step("ambience", lambda: L.sfx_ambience(job("sfx_ambience", set_code="SET01")))
    for code, c in chars.items():
        r = step(f"voice design {code}", lambda c=c: L.voice_design(job("voice_design", character_id=str(c["id"]), **{"try": 0})))
        print("   -> voice", r["voice_id"])
    # copy a few results where they can be looked at
    for r in db.fetch_all("select kind, r2_key_review, r2_key_clean, tags from character_assets where series_id = %s", (sid,)):
        name = f"{r['kind']}_{abs(hash(r['r2_key_clean'])) % 10000}"
        storage.get_to_file(r["r2_key_review"] or r["r2_key_clean"], out / (name + (".mp3" if r["kind"] == "voice_sample" else ".png")))
    storage.get_to_file(f"series/{sid}/sets/SET01_ambience.mp3", out / "ambience.mp3")
    print(f"TOTAL ${sum(cost.values()):.4f} in {sum(clock.values()):.0f}s | series {sid} | files in {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
