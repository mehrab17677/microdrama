"""Real-model smoke test of the story stages (phase 6). Costs a few dollars of Anthropic usage at most.

    DRY_RUN=0 EMBEDDING_BACKEND=local python scripts/smoke_story.py --out DIR [--episodes 2] [--language bn]

Seeds a throw-away library source, user and series in the dev database, then runs bible, beats, a few
scripts, continuity, wardrobe and originality for real, printing what came back and what it cost.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.jobs.handlers import story_handlers as H  # noqa: E402
from app.stages import ingest, story  # noqa: E402
from app.web import auth  # noqa: E402

LIBRARY_STORY = """Chapter 1

Mira Haldar came back to Sonargaon on the last ferry, carrying a cardboard suitcase and a letter she had not shown anyone. Eleven years earlier her brother Tanvir had signed away the family's riverside shop to a moneylender named Kabir Sarkar, and the village had watched her leave in shame the morning after.

Kabir was waiting at the ghat. He had grown heavier, richer, kinder-looking, which she found harder to bear than cruelty. "You are late," he said, as if she had been expected, and took her suitcase without asking.

Chapter 2

The letter said the shop had never been lost. Tanvir had hidden the deed inside the wall of the old tea stall before he died, and only Kabir's late wife knew where. Mira told no one. She took a room above the bakery run by Nilufar, who asked too many questions and fed her anyway.

Each evening Kabir found a reason to pass the bakery. He mended the shutter. He paid a debt that was not his. Mira tallied every kindness against the eleven years and the sum would not come out even.

Chapter 3

When the monsoon flooded the lower lane, the whole village worked through the night stacking sandbags, and Mira found herself passing bricks beside the man she meant to ruin. At dawn he handed her a rusted tin box. Inside was the deed, and a note in his wife's hand: give it back when she returns.

Kabir had known for years. He had been waiting for Mira to come home so he could keep a promise to a dead woman, and Mira understood that she had spent the whole summer hating the wrong story. She did not forgive him that morning. She sat beside him on the wet steps and, for the first time in eleven years, did not leave."""


def job(sid, kind, episode_id=None, **req):
    return {"id": uuid.uuid4(), "kind": kind, "series_id": sid, "episode_id": episode_id, "request": {"rev": 0, **req}}


def with_retries(fn, kind, sid, episode_id=None, **req):
    """Do what the graph does: on a validation failure record a failed job and try again (up to 3 attempts),
    so the next prompt shows the model what was wrong."""
    import hashlib

    last = None
    for attempt in range(1, 4):
        j = job(sid, kind, episode_id, **req)
        try:
            r = fn(j)
            if attempt > 1:
                print(f"   (passed on attempt {attempt})")
            return r
        except Exception as exc:  # noqa: BLE001
            last = exc
            print(f"   attempt {attempt} rejected: {str(exc)[:160]}")
            db.execute(
                "insert into jobs (series_id, episode_id, kind, idempotency_key, request, status, error) values (%s, %s, %s, %s, %s::jsonb, 'failed', %s)",
                (sid, episode_id, kind, hashlib.sha256(str(j['id']).encode()).hexdigest(), json.dumps(j["request"]), str(exc)))
    raise last


def resume(sid, args, out) -> int:
    cost, timings = {}, {}

    def stage(name, fn):
        t = time.time()
        r = fn()
        timings[name], cost[name] = time.time() - t, r.get("actual_cost", 0.0)
        print(f"{name}: {timings[name]:.0f}s ${cost[name]:.3f}")
        return r

    if not db.fetch_one("select 1 from episodes where series_id = %s limit 1", (sid,)):
        stage("beats", lambda: H.llm_beats(job(sid, "llm_beats")))
    eps = db.fetch_all("select id, number, beat from episodes where series_id = %s order by number", (sid,))
    print(f"beats: {len(eps)} episodes; sample: {eps[0]['beat']['summary'][:120]}")
    (out / "beats.json").write_text(json.dumps([e["beat"] for e in eps], ensure_ascii=False, indent=1))
    for e in eps[: args.episodes]:
        stage(f"script {e['number']}", lambda e=e: with_retries(H.llm_script, "llm_script", sid, e["id"], episode=e["number"]))
    stage("continuity", lambda: with_retries(H.llm_continuity, "llm_continuity", sid))
    stage("wardrobe", lambda: with_retries(H.llm_wardrobe, "llm_wardrobe", sid))
    stage("originality", lambda: with_retries(H.llm_originality, "llm_originality", sid))
    scripts = story.load_scripts(sid)
    (out / "scripts.json").write_text(json.dumps({k: v.model_dump() for k, v in scripts.items()}, ensure_ascii=False, indent=1))
    rep = db.fetch_one("select passed, detail from qa_results where series_id = %s and check_name = 'originality'", (sid,))
    print("originality:", rep["passed"], {k: v for k, v in rep["detail"].items() if k not in ("nearest_source_id",)})
    first = next(iter(scripts.values()))
    print("sample lines:", [l.text for l in first.scenes[0].lines[:3]])
    print(f"TOTAL ${sum(cost.values()):.3f} in {sum(timings.values()):.0f}s | series {sid}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--episodes", type=int, default=2, help="how many scripts to write")
    ap.add_argument("--language", default="bn")
    ap.add_argument("--series", help="reuse a series from an earlier run (skips setup and the bible)")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    tag = uuid.uuid4().hex[:6]
    cost, timings = {}, {}

    reuse = args.series and uuid.UUID(args.series)
    if reuse:
        return resume(reuse, args, out)
    # a library source, analysed by the real writer model
    genre = db.fetch_one("insert into genres (name, name_normalized) values (%s, %s) returning id", (f"Smoke revenge romance {tag}", f"smoke revenge romance {tag}"))["id"]
    src = db.fetch_one(
        "insert into sources (genre_id, batch_id, file_name, file_sha256, r2_key, status) values (%s, %s, 'story.txt', %s, 'smoke', 'queued') returning *",
        (genre, uuid.uuid4(), uuid.uuid4().hex))
    path = Path(tempfile.mkdtemp()) / "story.txt"
    path.write_text(LIBRARY_STORY)
    t = time.time()
    res = ingest.ingest(src, path)
    timings["ingest"], cost["ingest"] = time.time() - t, res["actual_cost"]
    print(f"library source: {res['chunks']} chunks, {res['pattern_cards']} cards, {res['blocklist_terms']} blocklist terms (${res['actual_cost']:.3f})")

    owner = db.fetch_one("insert into users (email, display_name, password_hash, role, status) values (%s, 'Smoke', %s, 'member', 'active') returning id",
                         (f"smoke-{tag}@example.com", auth.hash_password("x" * 12)))["id"]
    sid = uuid.uuid4()
    pinned = db.fetch_one("select max(lore_version) v from sources where status = 'ready'")["v"]
    db.execute("insert into series (id, owner_id, title, category, genre_id, language, status, current_stage, thread_id, pinned_lore_version) values (%s,%s,'Untitled','standard',%s,%s,'running','story',%s,%s)",
               (sid, owner, genre, args.language, str(sid), pinned))
    db.execute("insert into briefs (series_id, version, keywords, aesthetic_note) values (%s, 1, %s, %s)",
               (sid, ["revenge", "village", "slow burn", "forgiveness"], "warm amber light, river mist, handloom saris, monsoon greens"))
    db.execute("insert into brief_refs (series_id, kind, role_label, r2_key) values (%s, 'character', 'female lead', 'x')", (sid,))

    def stage(name, fn):
        t = time.time()
        r = fn()
        timings[name], cost[name] = time.time() - t, r.get("actual_cost", 0.0)
        print(f"{name}: {timings[name]:.0f}s ${cost[name]:.3f}")
        return r

    stage("bible", lambda: H.llm_bible(job(sid, "llm_bible")))
    bible = story.series_context(sid)["bible"]
    print(f"  title: {bible.title} | {bible.episode_count} episodes | {len(bible.characters)} characters | {len(bible.sets)} sets")
    (out / "bible.json").write_text(json.dumps(bible.model_dump(), ensure_ascii=False, indent=1))
    stage("beats", lambda: H.llm_beats(job(sid, "llm_beats")))
    eps = db.fetch_all("select id, number, beat from episodes where series_id = %s order by number", (sid,))
    (out / "beats.json").write_text(json.dumps([e["beat"] for e in eps], ensure_ascii=False, indent=1))
    for e in eps[: args.episodes]:
        stage(f"script {e['number']}", lambda e=e: H.llm_script(job(sid, "llm_script", e["id"], episode=e["number"])))
    stage("continuity", lambda: H.llm_continuity(job(sid, "llm_continuity")))
    stage("wardrobe", lambda: H.llm_wardrobe(job(sid, "llm_wardrobe")))
    r = stage("originality", lambda: H.llm_originality(job(sid, "llm_originality")))
    scripts = story.load_scripts(sid)
    (out / "scripts.json").write_text(json.dumps({k: v.model_dump() for k, v in scripts.items()}, ensure_ascii=False, indent=1))
    rep = db.fetch_one("select passed, detail from qa_results where series_id = %s and check_name = 'originality'", (sid,))
    print("originality:", rep["passed"], {k: v for k, v in rep["detail"].items() if k not in ("nearest_source_id",)})
    first = next(iter(scripts.values()))
    print("sample lines:", [l.text for l in first.scenes[0].lines[:3]])
    print(f"TOTAL ${sum(cost.values()):.3f} in {sum(timings.values()):.0f}s | series {sid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
