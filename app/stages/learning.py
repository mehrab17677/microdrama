"""Learning from QA (guide 11.6 and 11.7): learned rules and the camera library.

Rules   write (llm_rule) -> de-duplicate by embedding (cosine >= 0.88 increments hits) -> read by llm_shot_request (hits +1)
        -> score (a shot that passes QA on its first attempt adds a win to each rule it was given) -> retire weekly.
Cameras every finished shot adds a use; a first-attempt pass adds a pass; scored lip-sync adds to the score sum; the wording that worked
        is saved; a setup with at least 10 uses on one model and a pass rate under 0.5 learns to avoid that model.
The camera counters are updated once per shot (spec.learn_counted), so running the learn step again never double counts.
"""
from __future__ import annotations

from psycopg.types.json import Jsonb
from pydantic import BaseModel

from app import db
from app.media.routing import model_key
from app.vendors import images, llm

DEDUP_COSINE = 0.88
AVOID_MIN_USES = 10
AVOID_MAX_PASS_RATE = 0.5
RETIRE_MIN_HITS = 20
RETIRE_MAX_WIN_RATE = 0.5

RULE_INSTRUCTIONS = """You write one rule for an AI video model's prompt writer, from one failed shot.
You are given the model, the prompt that was sent, and what the check found. Write ONE sentence of at most 25 words saying what to avoid or do in future prompts
so this failure does not repeat. Be concrete and general: no character names, no brand names, no episode numbers."""


class RuleOut(BaseModel):
    rule: str


def first_attempt_pass(shot: dict) -> bool:
    return shot["status"] == "qa_passed" and shot["attempts"] == 0 and shot["sync_attempts"] == 0


def latest_request_result(shot_id, attempts: int | None = None) -> dict:
    """The newest successful llm_shot_request result for the shot (or the one written for render attempt `attempts`)."""
    sql, params = "select result from jobs where kind = 'llm_shot_request' and shot_id = %s and status = 'succeeded'", [shot_id]
    if attempts is not None:
        sql += " and (request->>'attempts')::int = %s"
        params.append(attempts)
    row = db.fetch_one(sql + " order by finished_at desc limit 1", params)
    return (row or {}).get("result") or {}


# ---------------------------------------------------------------- rules

def write_rule(shot: dict, check: str, fixture=None) -> tuple[RuleOut, dict]:
    mk = model_key(shot["tier"], shot["fallback_used"], shot["line_count"])
    fails = db.fetch_all("select category, detail from qa_results where shot_id = %s and check_name = %s and passed = false and category is distinct from 'unscored' order by qa_round desc, created_at desc", (shot["id"], check))
    attempt = fails[0]["detail"].get("attempts") if fails else None     # the prompt of the attempt that failed, not of a later repair
    prompt = (latest_request_result(shot["id"], attempt) if attempt is not None else {}).get("prompt") or latest_request_result(shot["id"]).get("prompt", "")
    detail = "; ".join(f"{f['category'] or ''} {f['detail']}" for f in fails[:2])
    material = f"Model: {mk}\nPrompt that was sent:\n{prompt}\n\nCheck: {check}\nWhat it found: {detail}"
    return llm.complete("rule_writer", RULE_INSTRUCTIONS, [{"role": "user", "content": material}], schema=RuleOut,
                        fixture=fixture or (lambda: RuleOut(rule=f"Describe a calm, steady subject and avoid {fails[0]['category'] if fails else check} problems.")))


def store_rule(model_key_: str, text: str, category: str, source_shot_id) -> tuple[str, bool, dict]:
    """Insert the rule, or increment hits on a near-duplicate. Returns (rule id, inserted, embedding usage)."""
    (vec,), usage = images.embed([text])
    near = db.fetch_one(
        "select id, 1 - (embedding <=> %s::vector) sim from learned_rules where active and model_key = %s and embedding is not null order by embedding <=> %s::vector limit 1",
        (db.to_vector(vec), model_key_, db.to_vector(vec)))
    if near and float(near["sim"]) >= DEDUP_COSINE:
        db.execute("update learned_rules set hits = hits + 1 where id = %s", (near["id"],))
        return str(near["id"]), False, usage
    row = db.fetch_one("insert into learned_rules (model_key, rule_text, category, source_shot_id, embedding) values (%s,%s,%s,%s,%s::vector) returning id",
                       (model_key_, text, category, source_shot_id, db.to_vector(vec)))
    return str(row["id"]), True, usage


def retire_rules() -> int:
    """Weekly: deactivate rules with hits >= 20 and wins / hits < 0.5."""
    rows = db.fetch_all("update learned_rules set active = false where active and hits >= %s and wins::float / hits < %s returning id", (RETIRE_MIN_HITS, RETIRE_MAX_WIN_RATE))
    return len(rows)


# ---------------------------------------------------------------- camera library and rule scores

def apply_learning(series_id, batch: str) -> dict:
    """The learn node: promote episodes to qa_passed, then update camera counters, rule wins and wording (once per shot)."""
    shots = db.fetch_all(
        "select s.*, e.number episode_number, cardinality(s.line_ids) line_count from shots s join episodes e on e.id = s.episode_id"
        " where s.series_id = %s and e.batch = %s and s.status in ('qa_passed','qa_failed','needs_human') and not coalesce((s.spec->>'learn_counted')::boolean, false)", (series_id, batch))
    wins = uses = 0
    with db.transaction() as conn:
        for s in shots:
            first = first_attempt_pass(s)
            result = latest_request_result(s["id"])
            if first:
                for rid in result.get("rule_ids") or []:
                    conn.execute("update learned_rules set wins = wins + 1 where id = %s", (rid,))
                    wins += 1
            if s["camera_setup_id"] and s["tier"] != "INSERT":   # an insert is built by FFmpeg: there is no model to learn about
                uses += 1
                conn.execute("update camera_setups set uses = uses + 1, passes = passes + %s where id = %s", (1 if first else 0, s["camera_setup_id"]))
                score = conn.execute("select score from qa_results where shot_id = %s and check_name = 'lipsync' and score is not null order by qa_round desc limit 1", (s["id"],)).fetchone()
                if score:
                    conn.execute("update camera_setups set lipsync_score_sum = lipsync_score_sum + %s, lipsync_score_n = lipsync_score_n + 1 where id = %s", (score["score"], s["camera_setup_id"]))
                mk = model_key(s["tier"], s["fallback_used"], s["line_count"])
                if first and result.get("prompt") and s["tier"] != "INSERT":
                    conn.execute("update camera_setups set prompt_by_model = prompt_by_model || %s where id = %s and not (prompt_by_model ? %s)",
                                 (Jsonb({mk: result["prompt"]}), s["camera_setup_id"], mk))
            conn.execute("update shots set spec = spec || '{\"learn_counted\": true}'::jsonb where id = %s", (s["id"],))
    avoided = update_avoid_models()
    return {"shots": len(shots), "uses": uses, "wins": wins, "avoid_added": avoided}


def update_avoid_models() -> int:
    """Append a model key to avoid_models when a setup has >= 10 finished uses on it and a first-attempt pass rate under 0.5."""
    stats: dict[tuple, list[int]] = {}
    for s in db.fetch_all(
        "select s.camera_setup_id, s.tier, s.fallback_used, cardinality(s.line_ids) line_count, s.status, s.attempts, s.sync_attempts from shots s"
        " where s.camera_setup_id is not null and s.tier <> 'INSERT' and s.status in ('qa_passed','qa_failed','needs_human')"):
        key = (s["camera_setup_id"], model_key(s["tier"], s["fallback_used"], s["line_count"]))
        entry = stats.setdefault(key, [0, 0])
        entry[0] += 1
        entry[1] += 1 if first_attempt_pass(s) else 0
    added = 0
    for (setup_id, mk), (n, ok) in stats.items():
        if n >= AVOID_MIN_USES and ok / n < AVOID_MAX_PASS_RATE:
            row = db.fetch_one("update camera_setups set avoid_models = array_append(avoid_models, %s) where id = %s and not (%s = any(avoid_models)) returning id", (mk, setup_id, mk))
            added += 1 if row else 0
    return added
