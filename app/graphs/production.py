"""The production graph (guide section 8).

Five rules for every node:
  1. A node never calls a vendor. It enqueues jobs.
  2. A node is idempotent: it selects work by status, so running it twice enqueues nothing new.
  3. A pause node contains one line: the interrupt() call.
  4. The node before a pause sets the series status (waiting_jobs / waiting_gate).
  5. The node after a pause reads state['last_resume'] and routes through a conditional edge.

A step X is two nodes: enq_X (enqueue) and wait_X (pause). A gate N is prepare_gateN (status + alert),
gateN (pause) and a conditional edge on the decision.
"""
from __future__ import annotations

from typing import Callable

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app import alerts, audit, db
from app.config import get_config
from app.graphs import common, work
from app.graphs.common import get_series, get_verdict, set_series, set_verdict
from app.graphs.state import ProductionState
from app.graphs.steps import enqueue_step
from app.jobs import ledger
from app.feedback import STORY_DECLINE
from app.media import durations
from app.media.routing import method_tier
from app.models.enums import AlertKind
from app.qa import face
from app.stages import look as look_stage
from app.stages import validation
from app.vendors import images

PER_SHOT_STEPS = {"keyframes", "requests", "clips", "lipsync"}
MAX_REPAIR_ROUNDS = 3   # guide 11.5: when round reaches 3, continue to learn
TERMINAL = ("failed", "discarded")


def _ids(state: ProductionState) -> tuple[str, str, int]:
    return state["series_id"], state.get("batch", "pilot"), state.get("round", 0)


def _dead(sid) -> bool:
    return get_series(sid)["status"] in TERMINAL


# ------------------------------------------------------------------ generic step and gate nodes

def make_wait(name: str):
    def node(state):
        payload = interrupt({"type": "job_wait", "step": name})
        return {"last_resume": payload}
    return node


def make_gate(n: int):
    def node(state):
        payload = interrupt({"type": "human_gate", "gate": n})
        return {"last_resume": payload}
    return node


def make_enq(name: str, stage: str | None, items_fn: Callable):
    def node(state):
        sid, batch, rnd = _ids(state)
        if _dead(sid):
            return {}
        if stage:
            set_series(sid, current_stage=stage)
        outcome, exhausted = enqueue_step(sid, name, batch, rnd, items_fn(sid, batch, rnd))
        if exhausted:
            if name in PER_SHOT_STEPS:  # isolated failure: the shot goes to a human, the batch carries on
                for item in exhausted:
                    if item.shot_id:
                        db.execute("update shots set status = 'needs_human' where id = %s", (item.shot_id,))
                        common.add_note(sid, "needs_human", f"{item.kind} failed {get_config('max_attempts')} times", shot_id=str(item.shot_id))
            else:
                common.fail_series(sid, name, f"{len(exhausted)} job(s) failed after {get_config('max_attempts')} attempts ({', '.join(sorted({str(i.kind) for i in exhausted}))})")
        return {}
    node.__name__ = f"enq_{name}"
    return node


def _resolve(next_: str | Callable, state) -> str:
    return next_(state) if callable(next_) else next_


def after_enq(name: str, next_):
    """Waiting on a group -> the wait node; nothing in flight -> straight on."""
    def edge(state):
        sid = state["series_id"]
        s = get_series(sid)
        if s["status"] in TERMINAL:
            return END
        if s["waiting_group"] is not None:
            return f"wait_{name}"
        return _resolve(next_, state)
    return edge


def after_wait(name: str, next_):
    def edge(state):
        sid = state["series_id"]
        s = get_series(sid)
        if s["status"] in TERMINAL:
            return END
        payload = state.get("last_resume") or {}
        if payload.get("type") != "jobs_done" or payload.get("group") != s["waiting_group"]:
            return f"wait_{name}"  # a stale resume: pause again until this step's own group finishes
        if payload.get("failed", 0) > 0:
            return f"enq_{name}"   # re-enqueue the failed work as a new attempt, or give up
        return _resolve(next_, state)
    return edge


def add_step(g: StateGraph, name: str, stage: str | None, items_fn: Callable, next_) -> None:
    g.add_node(f"enq_{name}", make_enq(name, stage, items_fn))
    g.add_node(f"wait_{name}", make_wait(name))
    g.add_conditional_edges(f"enq_{name}", after_enq(name, next_))
    g.add_conditional_edges(f"wait_{name}", after_wait(name, next_))


def make_prepare_gate(n: int, stage_note: str, link: str, before: Callable | None = None):
    def node(state):
        sid, batch, _ = _ids(state)
        if _dead(sid):
            return {}
        s = get_series(sid)
        already = s["status"] == "waiting_gate" and s["current_stage"] == f"gate{n}"
        if before and not already:
            before(sid, batch)
        with db.transaction() as conn:
            conn.execute("update series set status = 'waiting_gate', current_stage = %s, waiting_group = null, updated_at = now() where id = %s", (f"gate{n}", sid))
            if not already:
                alerts.notify(common.recipients(conn, sid), AlertKind.gate_open, stage_note, link.format(id=sid), sid, conn=conn)
        return {}
    node.__name__ = f"prepare_gate{n}"
    return node


def gate_payload(state, gate: int) -> dict | None:
    p = state.get("last_resume") or {}
    return p if p.get("type") == "gate" and p.get("gate") == gate else None


# ------------------------------------------------------------------ story stage nodes

def intake_check(state):
    from app import intake

    sid, _, _ = _ids(state)
    if _dead(sid):
        return {}
    problems = intake.check_series(sid)
    if problems:
        common.fail_series(sid, "intake_check", " ".join(problems))
        set_verdict(sid, "intake_check", "invalid")
        return {}
    series = get_series(sid)
    if series["genre_id"] is None:  # a user-supplied story must pass the model's completeness check
        check = db.fetch_one("select result from jobs where series_id = %s and kind = 'llm_story_check' and status = 'succeeded' order by finished_at desc limit 1", (sid,))
        r = (check or {}).get("result") or {}
        missing = [label for key, label in (("has_named_leads", "named leads"), ("has_conflict", "a central conflict"), ("has_beginning_middle_end", "a beginning, middle and ending")) if not r.get(key)]
        if missing or not check:
            common.add_note(sid, "story_missing", "Your story is missing: " + ", ".join(missing or ["a readable story"]) + ". Edit it and continue.")
            set_verdict(sid, "intake_check", "story")
            return {}
    set_verdict(sid, "intake_check", "ok")
    return {}


def route_intake(state):
    sid = state["series_id"]
    if _dead(sid):
        return END
    return "prepare_gate1" if get_verdict(sid, "intake_check") == "story" else "enq_bible"


def prepare_gate1(state):
    sid, _, _ = _ids(state)
    if _dead(sid):
        return {}
    text = common.latest_note(sid, "story_missing")
    s = get_series(sid)
    already = s["status"] == "waiting_gate" and s["current_stage"] == "gate1"
    with db.transaction() as conn:
        conn.execute("update series set status = 'waiting_gate', current_stage = 'gate1', waiting_group = null, updated_at = now() where id = %s", (sid,))
        if not already:
            alerts.notify(common.recipients(conn, sid), AlertKind.gate_open, text, f"/productions/{sid}", sid, conn=conn)
    return {}


def route_gate1(state):
    return "enq_story_ingest" if gate_payload(state, 1) else "gate1"


def validate_beats(state):
    """Code checks on the beat sheet. A failure sends the step round again with the errors in the
    request, twice at most (so at most three llm_beats runs in the current revision)."""
    sid, _, _ = _ids(state)
    if _dead(sid):
        return {}
    errors = validation.validate_beat_rows(sid)
    if not errors:
        if common.latest_note(sid, "beats_errors"):
            common.add_note(sid, "beats_errors", "")
        return {}
    runs = db.fetch_one(
        "select count(*) n from jobs where series_id = %s and kind = 'llm_beats' and status = 'succeeded' and (request->>'rev')::int = %s",
        (sid, common.gate_revs(sid, 2)),
    )["n"]
    if runs >= 3:
        common.fail_series(sid, "validate_beats", "the beat sheet kept failing validation: " + "; ".join(errors[:5]))
    else:
        common.add_note(sid, "beats_errors", "; ".join(errors))
        common.add_note(sid, "beats_redo", "; ".join(errors))
    return {}


def route_validate_beats(state):
    sid = state["series_id"]
    if _dead(sid):
        return END
    return "enq_beats" if validation.validate_beat_rows(sid) else "enq_scripts"


def after_beats(state):
    return "enq_placement" if get_series(state["series_id"])["category"] == "ad_placement" else "validate_beats"


def route_gate2(state):
    p = gate_payload(state, 2)
    if not p:
        return "gate2"
    return "enq_traits" if p["decision"] == "approve" else "reject_story"


def reject_story(state):
    """Reset what the rejected target produced so its step runs again (reviewer notes are already stored)."""
    sid, _, _ = _ids(state)
    target = (gate_payload(state, 2) or {}).get("target")
    with db.transaction() as conn:
        if target == "bible":
            conn.execute("delete from episodes where series_id = %s", (sid,))
            conn.execute("delete from characters where series_id = %s", (sid,))
            conn.execute("delete from sets where series_id = %s", (sid,))
        elif target == "beats":
            conn.execute("delete from episodes where series_id = %s", (sid,))
        elif target == "scripts":
            conn.execute("delete from dialogue_lines where series_id = %s", (sid,))
            conn.execute("update episodes set status = 'outlined', script = null, runtime_seconds = null where series_id = %s", (sid,))
            conn.execute("delete from wardrobe_items where series_id = %s", (sid,))
    return {}


def route_reject_story(state):
    return {"bible": "enq_bible", "beats": "enq_beats", "scripts": "enq_scripts"}.get((gate_payload(state, 2) or {}).get("target"), "enq_bible")


# ------------------------------------------------------------------ look stage nodes

def _char_refs(sid) -> dict:
    return look_stage.character_refs(sid)


def likeness_check(state):
    """A master portrait too close to the reference photo is regenerated: twice at most, the second
    time without the photo. After that it goes on and the score is shown at Gate 3."""
    sid, _, _ = _ids(state)
    if _dead(sid):
        return {}
    limit = get_config("qa_thresholds")["likeness_max"]
    for char_id, ref in _char_refs(sid).items():
        master = db.fetch_one("select id, face_vector::text fv from character_assets where character_id = %s and kind = 'master' order by version desc limit 1", (char_id,))
        if not master or master["fv"] is None or ref["face_vector"] is None:
            continue
        sim = face.similarity(_vec(master["fv"]), _vec(ref["face_vector"]))
        passed = sim < limit
        with db.transaction() as conn:
            conn.execute("delete from qa_results where series_id = %s and check_name = 'likeness' and detail->>'character_id' = %s", (sid, str(char_id)))
            conn.execute(
                "insert into qa_results (series_id, qa_round, check_name, passed, score, detail) values (%s, 'precheck', 'likeness', %s, %s, %s::jsonb)",
                (sid, passed, sim, f'{{"character_id": "{char_id}"}}'),
            )
            if not passed and common.count_notes(sid, f"likeness_retry:{char_id}") < 2:
                conn.execute("delete from character_assets where id = %s", (master["id"],))
                audit.log(conn, None, f"note:likeness_retry:{char_id}", sid, {"text": f"similarity {sim:.2f}"})
    return {}


def _vec(text) -> list[float]:
    if isinstance(text, (list, tuple)):
        return [float(x) for x in text]
    return [float(x) for x in str(text).strip("[]").split(",")]


def route_likeness(state):
    sid = state["series_id"]
    if _dead(sid):
        return END
    return "enq_masters" if work.masters_items(sid, "", 0) else "enq_emotion_groups"


def pack_check(state):
    """Each pack image must look like its master (similarity at least pack_match_min). Failing images
    are regenerated, twice at most."""
    sid, _, _ = _ids(state)
    if _dead(sid):
        return {}
    skipped = sorted(images.unsupported_pack_kinds())
    if skipped and not common.count_notes(sid, "pack_skipped"):
        with db.transaction() as conn:
            audit.log(conn, None, "note:pack_skipped", sid, {"text": "Character sheets skipped (image backend cannot make them): " + ", ".join(skipped)})
    floor = get_config("qa_thresholds")["pack_match_min"]
    masters = {r["character_id"]: _vec(r["fv"]) for r in db.fetch_all(
        "select character_id, face_vector::text fv from character_assets where series_id = %s and kind = 'master' and face_vector is not null", (sid,))}
    failed = []
    for a in db.fetch_all("select id, character_id, face_vector::text fv from character_assets where series_id = %s and kind not in ('master','voice_sample','lineup') and face_vector is not null", (sid,)):
        m = masters.get(a["character_id"])
        if m is not None and face.similarity(_vec(a["fv"]), m) < floor:
            failed.append(a["id"])
    if failed and common.count_notes(sid, "pack_retry") < 2:
        with db.transaction() as conn:
            conn.execute("delete from character_assets where id = any(%s)", (failed,))
            audit.log(conn, None, "note:pack_retry", sid, {"text": f"{len(failed)} image(s) did not match the master"})
    return {}


def route_pack_check(state):
    sid = state["series_id"]
    if _dead(sid):
        return END
    return "enq_packs" if work.pack_items(sid, "", 0) else "prepare_gate3"


def route_gate3(state):
    p = gate_payload(state, 3)
    if not p:
        return "gate3"
    return "lock_look" if p["decision"] == "approve" else "reject_look"


def reject_look(state):
    """Delete the listed assets so enq_packs makes them again; the notes and rev change the request."""
    sid, _, _ = _ids(state)
    ids = [i.strip() for i in ((gate_payload(state, 3) or {}).get("target") or "").split(",") if i.strip()]
    if ids:
        db.execute("delete from character_assets where series_id = %s and id::text = any(%s)", (sid, ids))
    return {}


def lock_look(state):
    """Mark every asset approved and store the chosen voice per character."""
    sid, _, _ = _ids(state)
    voices = (gate_payload(state, 3) or {}).get("voices") or {}
    with db.transaction() as conn:
        conn.execute("update character_assets set approved = true where series_id = %s", (sid,))
        for code, voice_id in voices.items():
            conn.execute("update characters set voice_id = %s where series_id = %s and code = %s", (voice_id, sid, code))
    return {}


# ------------------------------------------------------------------ build stage nodes

def start_batch(state):
    sid, batch, _ = _ids(state)
    if _dead(sid):
        return {}
    set_series(sid, batch=batch, current_stage="build", status="running", waiting_group=None)
    return {}


def to_full_batch(state):
    return {"batch": "full", "round": 0}


def _redo_shot_list(conn, sid, episode, message: str) -> None:
    """Send an episode back to the shot-list step (atomic): shots deleted, status 'scripted', errors noted."""
    audit.log(conn, None, f"note:shots_errors:{episode['number']}", sid, {"text": message})
    audit.log(conn, None, f"note:shots_redo:{episode['number']}", sid, {"text": message})
    conn.execute("delete from shots where episode_id = %s", (episode["id"],))
    conn.execute("update episodes set status = 'scripted' where id = %s", (episode["id"],))


def validate_shots(state):
    sid, batch, _ = _ids(state)
    if _dead(sid):
        return {}
    for e in common.batch_episodes(sid, batch, ("shots_planned",)):
        errors = validation.validate_shot_rows(e["id"])
        if not errors:
            continue
        if common.count_notes(sid, f"shots_redo:{e['number']}") >= 2:
            common.fail_series(sid, "validate_shots", f"episode {e['number']} shot list kept failing: " + "; ".join(errors[:4]))
            return {}
        with db.transaction() as conn:
            _redo_shot_list(conn, sid, e, "; ".join(errors))
    return {}


def _needs_shot_list(sid, batch) -> bool:
    return bool(common.batch_episodes(sid, batch, ("scripted",)))


def route_validate_shots(state):
    sid, batch, _ = _ids(state)
    if _dead(sid):
        return END
    return "enq_shot_lists" if _needs_shot_list(sid, batch) else "enq_audio_tags"


def fit_durations(state):
    """Speech first, then picture: each spoken line sets the length of its shot (guide 10.2)."""
    sid, batch, _ = _ids(state)
    if _dead(sid):
        return {}
    timing = get_config("timing")
    for e in common.batch_episodes(sid, batch, ("shots_planned",)):
        lines = db.fetch_all("select id, duration_seconds from dialogue_lines where episode_id = %s", (e["id"],))
        if any(l["duration_seconds"] is None for l in lines):
            continue  # a line failed to voice; the step that owned it already reported it
        dur = {l["id"]: float(l["duration_seconds"]) for l in lines}
        shots = db.fetch_all("select id, idx, tier, line_ids, spec from shots where episode_id = %s order by idx", (e["id"],))
        fitted = durations.fit_episode(
            [{"idx": s["idx"], "tier": s["tier"], "line_durations": [dur[i] for i in s["line_ids"]], "est_seconds": s["spec"].get("est_seconds"), "fixed": s["spec"].get("placement_kind") in ("phone_full", "product_full")} for s in shots], timing)
        if fitted["too_long"]:
            if common.count_notes(sid, f"shots_redo:{e['number']}") >= 2:
                common.fail_series(sid, "fit_durations", f"episode {e['number']} still has a shot over 10 s after redoing the shot list")
                return {}
            msg = "; ".join(f"shot {i} needs {fitted['planned'][i]:.1f} s; split it" for i in fitted["too_long"])
            with db.transaction() as conn:
                _redo_shot_list(conn, sid, e, msg)
            continue
        with db.transaction() as conn:
            for s in shots:
                conn.execute("update shots set planned_duration = %s, render_duration = %s where id = %s", (fitted["planned"][s["idx"]], fitted["render"][s["idx"]], s["id"]))
            conn.execute("update episodes set runtime_seconds = %s, status = 'voiced' where id = %s", (fitted["runtime"], e["id"]))
            conn.execute("delete from qa_results where episode_id = %s and check_name = 'runtime' and qa_round = 'precheck'", (e["id"],))
            if not fitted["in_range"]:  # reported at the gate (the script redo arrives with phase 8)
                conn.execute(
                    "insert into qa_results (series_id, episode_id, qa_round, check_name, passed, score, detail) values (%s, %s, 'precheck', 'runtime', false, %s, '{}'::jsonb)",
                    (sid, e["id"], fitted["runtime"]),
                )
    return {}


def route_fit_durations(state):
    sid, batch, _ = _ids(state)
    if _dead(sid):
        return END
    return "enq_shot_lists" if _needs_shot_list(sid, batch) else "enq_keyframes"


def keyframe_check(state):
    """Each keyframe must show the character's master face (similarity at least face_match_min).
    Failures are regenerated, twice at most. No detectable face (back to camera, wide shot) passes."""
    sid, batch, _ = _ids(state)
    if _dead(sid):
        return {}
    from app.vendors import storage
    import tempfile, pathlib

    floor = get_config("qa_thresholds")["face_match_min"]
    masters = {r["code"]: _vec(r["fv"]) for r in db.fetch_all(
        "select c.code, a.face_vector::text fv from character_assets a join characters c on c.id = a.character_id"
        " where a.series_id = %s and a.kind = 'master' and a.face_vector is not null", (sid,))}
    cache: dict[str, float | None] = {}
    redo = False
    can_retry = common.count_notes(sid, "keyframe_retry") < 2
    with tempfile.TemporaryDirectory() as tmp:
        for s in common.batch_shots(sid, batch, ("keyframe_ready",)):
            code = next((c["code"] for c in s["spec"].get("characters", []) if c.get("mouth_visible_speaking")), None) or next((c["code"] for c in s["spec"].get("characters", [])), None)
            key = s["keyframe_r2_key"]
            if not key or code not in masters:
                continue
            if key not in cache:
                path = pathlib.Path(tmp) / "kf.png"
                storage.get_to_file(key, path)
                vec = face.face_vector(path.read_bytes())
                cache[key] = None if vec is None else face.similarity(vec, masters[code])
            sim = cache[key]
            passed = sim is None or sim >= floor
            db.execute("delete from qa_results where shot_id = %s and qa_round = 'precheck' and check_name = 'keyframe_face'", (s["id"],))
            db.execute("insert into qa_results (series_id, episode_id, shot_id, qa_round, check_name, passed, score, detail) values (%s, %s, %s, 'precheck', 'keyframe_face', %s, %s, %s::jsonb)",
                       (sid, s["episode_id"], s["id"], passed, sim, '{"no_face": true}' if sim is None else "{}"))
            if not passed and can_retry:
                db.execute("update shots set status = 'planned', attempts = attempts + 1, keyframe_r2_key = null where id = %s", (s["id"],))
                redo = True
    if redo:
        common.add_note(sid, "keyframe_retry", "regenerating keyframes that lost the face")
    db.execute(
        "update episodes set status = 'keyframed' where series_id = %s and batch = %s and status = 'voiced' and not exists"
        " (select 1 from shots s where s.episode_id = episodes.id and s.tier <> 'INSERT' and s.keyframe_r2_key is null)", (sid, batch))
    return {}


def route_keyframe_check(state):
    sid, batch, _ = _ids(state)
    if _dead(sid):
        return END
    planned = common.batch_shots(sid, batch, ("planned",))
    return "enq_keyframes" if any(s["tier"] != "INSERT" for s in planned) else "enq_requests"


def pre_compile(state):
    """Episodes whose shots are all ready become 'rendered' (the compile step selects those)."""
    sid, batch, _ = _ids(state)
    db.execute(
        "update episodes set status = 'rendered' where series_id = %s and batch = %s and status in ('voiced','keyframed') and not exists"
        " (select 1 from shots s where s.episode_id = episodes.id and s.status not in ('ready','qa_passed','needs_human'))"
        " and not exists (select 1 from shots s where s.episode_id = episodes.id and s.status = 'needs_human')", (sid, batch))


def plan_repairs(state):
    """One action per failed shot (guide 11.5, app/qa/repair.py). Reset shots send their episode back to 'rendered'
    and the round counter goes up; the loop ends when round reaches 3 or nothing was reset. Shots that ran out of
    attempts become needs_human. Episode-level failures (runtime, continuity) are not auto-repaired."""
    from app.qa import repair

    sid, batch, rnd = _ids(state)
    if _dead(sid):
        return {}
    if rnd >= MAX_REPAIR_ROUNDS:
        return {}
    reset = 0
    with db.transaction() as conn:
        for s in common.batch_shots(sid, batch, ("qa_failed",)):
            plan = repair.decide(s, repair.failures(s["id"]), int(get_config("max_attempts")))
            if plan is None:
                conn.execute("update shots set status = 'qa_passed' where id = %s", (s["id"],))   # only unscored checks failed
                continue
            repair.apply(conn, s, plan)
            audit.log(conn, None, f"note:repair:{s['episode_number']}:{s['idx']}", sid, {"text": plan["action"]})
            if plan["reset"]:
                reset += 1
                conn.execute("update episodes set status = 'rendered' where id = %s", (s["episode_id"],))
    return {"round": rnd + 1} if reset else {}


def route_plan_repairs(state):
    sid, batch, _ = _ids(state)
    if _dead(sid):
        return END
    pending = [e for e in common.batch_episodes(sid, batch, ("rendered",))
               if not db.fetch_one("select 1 from shots where episode_id = %s and status = 'needs_human' limit 1", (e["id"],))]   # a needs_human shot blocks its episode
    return "enq_keyframes" if pending else "enq_learn"


def apply_learning(state):
    """After the learn jobs: QA is finished for the batch. Episodes become qa_passed (flagged shots stay listed for the gate),
    then camera counters, rule wins and saved wording are updated (app/stages/learning.py)."""
    from app.stages import learning

    sid, batch, _ = _ids(state)
    if _dead(sid):
        return {}
    db.execute(
        "update episodes set status = 'qa_passed' where series_id = %s and batch = %s and (status = 'compiled' or (status = 'rendered' and exists"
        " (select 1 from shots s where s.episode_id = episodes.id and s.status = 'needs_human')))", (sid, batch))
    learning.apply_learning(sid, batch)
    from app import placement

    placement.finalize(sid, batch)
    return {}


def after_learn(state):
    return "prepare_gate4" if state.get("batch", "pilot") == "pilot" else "prepare_gate5"


def before_review_gate(sid, batch):
    """QA finished: alert the owner with the number of flagged shots, and close feedback items whose
    episodes now pass."""
    flagged = len(common.batch_shots(sid, batch, ("qa_failed", "needs_human")))
    common.notify_series(sid, AlertKind.qa_done, f"QA finished: {flagged} flagged shot(s).", with_admin=False)
    done = db.fetch_all(
        "select f.id, f.created_by from feedback_items f where f.series_id = %s and f.status = 'in_progress' and not exists"
        " (select 1 from episodes e where e.series_id = f.series_id and e.number = f.episode_number and e.status not in ('qa_passed','approved'))", (sid,))
    with db.transaction() as conn:
        for f in done:
            conn.execute("update feedback_items set status = 'fixed' where id = %s", (f["id"],))
            alerts.notify([f["created_by"]], AlertKind.feedback_status, "Your feedback item was fixed.", f"/productions/{sid}", sid, conn=conn)


# ------------------------------------------------------------------ decisions and delivery nodes

def route_gate4(state):
    p = gate_payload(state, 4)
    if not p:
        return "gate4"
    return {"continue": "to_full_batch", "discard": "close_discarded", "feedback": "apply_feedback"}.get(p["decision"], "gate4")


def route_gate5(state):
    p = gate_payload(state, 5)
    if not p:
        return "gate5"
    return {"approve": "enq_delivery", "feedback": "apply_feedback"}.get(p["decision"], "gate5")


def close_discarded(state):
    sid, _, _ = _ids(state)
    p = gate_payload(state, 4) or {}
    with db.transaction() as conn:
        ledger.cancel_series_jobs(sid, conn=conn)
        conn.execute("update series set status = 'discarded', waiting_group = null, discard_category = %s, discard_notes = %s, updated_at = now() where id = %s",
                     (p.get("category"), p.get("notes"), sid))
        audit.log(conn, None, "series.discarded", sid, {"category": p.get("category")})
    return {}


def apply_feedback(state):
    """Rework for confirmed feedback items (guide 12.6), by category. Each affected episode goes back to 'rendered' (or 'shots_planned' when a
    line is re-voiced, so the durations are fitted again) and each item to 'in_progress'. The item id is stored on its shots (spec.feedback_id)
    and goes into every job request, so the ledger keys are new. An episode from an earlier batch that is reworked at Gate 5 joins the current batch."""
    sid, batch, _ = _ids(state)
    ids = ((state.get("last_resume") or {}).get("feedback_ids")) or []
    from psycopg.types.json import Jsonb

    with db.transaction() as conn:
        for f in conn.execute("select * from feedback_items where series_id = %s and id::text = any(%s) and status = 'confirmed' order by created_at", (sid, ids)).fetchall():
            cat = f["category"] or "picture"
            ep = conn.execute("select id, batch from episodes where series_id = %s and number = %s", (sid, f["episode_number"])).fetchone()
            if cat == "story":
                conn.execute("update feedback_items set status = 'declined', decline_reason = %s where id = %s", (STORY_DECLINE, f["id"]))
                alerts.notify([f["created_by"]], AlertKind.feedback_status, "Story feedback needs a new production.", f"/productions/{sid}", sid, conn=conn)
                continue
            if ep and ep["batch"] != batch:
                conn.execute("update episodes set batch = %s where id = %s", (batch, ep["id"]))
            fb = Jsonb({"feedback_id": str(f["id"])})
            status_for_episode = "rendered"
            for s in conn.execute("select * from shots where id = any(%s)", (f["shot_ids"],)).fetchall():
                if cat in ("picture", "brand"):
                    new = "keyframe_ready" if s["tier"] == "INSERT" else "planned"   # an insert has no keyframe: it is rebuilt from the plate
                    conn.execute("update shots set status = %s, attempts = 0, sync_attempts = 0, fallback_used = false, keyframe_r2_key = null, raw_clip_r2_key = null,"
                                 " final_clip_r2_key = null, spec = spec || %s where id = %s", (new, fb, s["id"]))
                elif cat == "lipsync":
                    new = "rendered" if method_tier(s["tier"], s["fallback_used"], len(s["line_ids"])) in ("B2", "C") else "keyframe_ready"
                    conn.execute("update shots set status = %s, sync_attempts = 0, spec = spec || %s where id = %s", (new, fb, s["id"]))
                elif cat == "voice":
                    conn.execute("update dialogue_lines set version = version + 1, audio_r2_key = null, audio_tag = null, duration_seconds = null, alignment = null where id = any(%s)", (s["line_ids"],))
                    conn.execute("update shots set status = 'keyframe_ready', spec = spec || %s where id = %s", (fb, s["id"]))
                    status_for_episode = "shots_planned"
            if cat == "voice":
                audit.log(conn, None, f"note:voice_note:{f['episode_number']}", sid, {"text": f["note"]})
            if cat == "music" and ep:
                conn.execute("update episodes set music_r2_key = null where id = %s", (ep["id"],))
                audit.log(conn, None, f"note:music_note:{f['episode_number']}", sid, {"text": f["note"]})
            # sound and caption: nothing is rendered again, the compile builds the shot audio and the captions afresh
            if ep:
                conn.execute("update episodes set status = %s where id = %s", (status_for_episode, ep["id"]))
            conn.execute("update feedback_items set status = 'in_progress' where id = %s", (f["id"],))
            alerts.notify([f["created_by"]], AlertKind.feedback_status, "Your feedback is being worked on.", f"/productions/{sid}", sid, conn=conn)
    set_series(sid, status="running", current_stage="build", waiting_group=None)
    return {}


def finish(state):
    sid, _, _ = _ids(state)
    if _dead(sid):
        return {}
    db.execute("update episodes set status = 'approved' where series_id = %s and status = 'qa_passed'", (sid,))
    set_series(sid, status="done", current_stage="done", waiting_group=None)
    common.notify_series(sid, AlertKind.action_finished, "Your production is finished.", f"/productions/{sid}/delivery", with_admin=False)
    return {}


# ------------------------------------------------------------------ graph

def build_production_graph() -> StateGraph:
    g = StateGraph(ProductionState)
    W = work

    # --- story
    add_step(g, "story_ingest", "story", W.story_ingest_items, "intake_check")
    g.add_node("intake_check", intake_check)
    g.add_conditional_edges("intake_check", route_intake)
    g.add_node("prepare_gate1", prepare_gate1)
    g.add_node("gate1", make_gate(1))
    g.add_edge("prepare_gate1", "gate1")
    g.add_conditional_edges("gate1", route_gate1)
    add_step(g, "bible", "story", W.bible_items, "enq_beats")
    add_step(g, "beats", "story", W.beats_items, after_beats)
    add_step(g, "placement", "story", W.placement_items, "validate_beats")
    g.add_node("validate_beats", validate_beats)
    g.add_conditional_edges("validate_beats", route_validate_beats)
    add_step(g, "scripts", "story", W.scripts_items, "enq_continuity")
    add_step(g, "continuity", "story", W.continuity_items, "enq_story_checks")
    add_step(g, "story_checks", "story", W.story_check_items, "prepare_gate2")
    g.add_node("prepare_gate2", make_prepare_gate(2, "The story is ready for your review.", "/productions/{id}/review/story"))
    g.add_node("gate2", make_gate(2))
    g.add_edge("prepare_gate2", "gate2")
    g.add_conditional_edges("gate2", route_gate2)
    g.add_node("reject_story", reject_story)
    g.add_conditional_edges("reject_story", route_reject_story)

    # --- look
    add_step(g, "traits", "visual", W.traits_items, "enq_masters")
    add_step(g, "masters", "visual", W.masters_items, "likeness_check")
    g.add_node("likeness_check", likeness_check)
    g.add_conditional_edges("likeness_check", route_likeness)
    add_step(g, "emotion_groups", "visual", W.emotion_group_items, "enq_packs")
    add_step(g, "packs", "visual", W.pack_items, "pack_check")
    g.add_node("pack_check", pack_check)
    g.add_conditional_edges("pack_check", route_pack_check)
    g.add_node("prepare_gate3", make_prepare_gate(3, "The look is ready for your review.", "/productions/{id}/review/look"))
    g.add_node("gate3", make_gate(3))
    g.add_edge("prepare_gate3", "gate3")
    g.add_conditional_edges("gate3", route_gate3)
    g.add_node("reject_look", reject_look)
    g.add_edge("reject_look", "enq_packs")
    g.add_node("lock_look", lock_look)
    g.add_edge("lock_look", "start_batch")

    # --- build
    g.add_node("start_batch", start_batch)
    g.add_edge("start_batch", "enq_shot_lists")
    add_step(g, "shot_lists", "build", W.shot_list_items, "validate_shots")
    g.add_node("validate_shots", validate_shots)
    g.add_conditional_edges("validate_shots", route_validate_shots)
    add_step(g, "audio_tags", "build", W.audio_tag_items, "enq_tts")
    add_step(g, "tts", "build", W.tts_items, "fit_durations")
    g.add_node("fit_durations", fit_durations)
    g.add_conditional_edges("fit_durations", route_fit_durations)
    add_step(g, "keyframes", "build", W.keyframe_items, "keyframe_check")
    g.add_node("keyframe_check", keyframe_check)
    g.add_conditional_edges("keyframe_check", route_keyframe_check)
    add_step(g, "requests", "build", W.shot_request_items, "enq_clips")
    add_step(g, "clips", "build", _clips_with_budget, "enq_lipsync")
    add_step(g, "lipsync", "build", W.lipsync_items, "enq_sound")
    add_step(g, "sound", "build", W.sound_items, "enq_compile")
    add_step(g, "compile", "build", _compile_items, "enq_qa")
    add_step(g, "qa", "qa", W.qa_items, "plan_repairs")
    g.add_node("plan_repairs", plan_repairs)
    g.add_conditional_edges("plan_repairs", route_plan_repairs)
    add_step(g, "learn", "qa", W.learn_items, "apply_learning")
    g.add_node("apply_learning", apply_learning)
    g.add_conditional_edges("apply_learning", after_learn)

    # --- decisions and delivery
    g.add_node("prepare_gate4", make_prepare_gate(4, "The pilot is ready for your review.", "/productions/{id}/review/episodes", before_review_gate))
    g.add_node("gate4", make_gate(4))
    g.add_edge("prepare_gate4", "gate4")
    g.add_conditional_edges("gate4", route_gate4)
    g.add_node("to_full_batch", to_full_batch)
    g.add_edge("to_full_batch", "start_batch")
    g.add_node("close_discarded", close_discarded)
    g.add_edge("close_discarded", END)
    g.add_node("prepare_gate5", make_prepare_gate(5, "The final cut is ready for your review.", "/productions/{id}/review/episodes", before_review_gate))
    g.add_node("gate5", make_gate(5))
    g.add_edge("prepare_gate5", "gate5")
    g.add_conditional_edges("gate5", route_gate5)
    g.add_node("apply_feedback", apply_feedback)
    g.add_edge("apply_feedback", "enq_audio_tags")
    add_step(g, "delivery", "delivery", W.delivery_items, "finish")
    g.add_node("finish", finish)
    g.add_edge("finish", END)

    g.add_edge(START, "enq_story_ingest")
    return g


def _clips_with_budget(sid, batch, rnd):
    """The budget row is created the first time clips are enqueued for a batch (guide 7.5)."""
    from app import budget

    items = work.clip_items(sid, batch, rnd)
    if items:
        with db.transaction() as conn:
            budget.create_budget(conn, sid, batch, work.planned_video_cost(sid, batch))
    return items


def _compile_items(sid, batch, rnd):
    pre_compile({"series_id": sid, "batch": batch, "round": rnd})
    return work.compile_items(sid, batch, rnd)
