"""Deciding a gate (guide 12.5) and the pilot (decide_pilot). Every deciding route runs one of these."""
from __future__ import annotations

import json

import psycopg
from psycopg.types.json import Jsonb

from app import audit
from app.graphs import common

DISCARD_CATEGORIES = ("story", "look", "voice", "quality", "cost")
REJECT_TARGETS = ("bible", "beats", "scripts")


class DecisionError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def _commit(conn, series_id, user_id, gate: int, decision: str, notes: str | None, payload: dict, audit_detail: dict | None = None) -> None:
    """The transaction from guide 12.5. The caller has already locked the series row and checked the gate."""
    rnd = conn.execute("select coalesce(max(round), 0) + 1 r from gate_decisions where series_id = %s and gate = %s", (series_id, gate)).fetchone()["r"]
    try:
        with conn.transaction():  # a savepoint, so a duplicate decision can be reported as 409
            conn.execute("insert into gate_decisions (series_id, gate, round, decision, notes, decided_by) values (%s, %s, %s, %s, %s, %s)",
                         (series_id, gate, rnd, decision, notes or None, user_id))
    except psycopg.errors.UniqueViolation:
        raise DecisionError(409, "This gate was already decided.") from None
    conn.execute("update series set status = 'running', updated_at = now() where id = %s", (series_id,))
    row = conn.execute("select thread_id from series where id = %s", (series_id,)).fetchone()
    conn.execute(
        "insert into graph_commands (series_id, graph, thread_id, kind, payload, created_at) values (%s, 'production', %s, 'resume', %s, clock_timestamp())",
        (series_id, row["thread_id"], Jsonb(payload)),
    )
    audit.log(conn, user_id, "gate.decide", series_id, {"gate": gate, "decision": decision, **(audit_detail or {})})


def _lock_gate(conn, series_id, gate: int) -> dict:
    s = conn.execute("select * from series where id = %s for update", (series_id,)).fetchone()
    if s is None:
        raise DecisionError(404, "Production not found.")
    if s["status"] != "waiting_gate" or s["current_stage"] != f"gate{gate}":
        raise DecisionError(409, "This gate was already decided.")
    return s


def decide_gate(conn, series_id, user_id, gate: int, decision: str, notes: str = "", target: str = "", voices: dict | None = None, story_text: str | None = None) -> dict:
    """resume_gate for gates 1, 2, 3 and 5. Returns the payload sent to the graph."""
    if gate not in (1, 2, 3, 5):
        raise DecisionError(400, "Gates 0 and 4 are not decided here.")
    s = _lock_gate(conn, series_id, gate)
    notes, target = (notes or "").strip(), (target or "").strip()
    allowed = {1: ("approve",), 2: ("approve", "reject"), 3: ("approve", "reject"), 5: ("approve",)}[gate]
    if decision not in allowed:
        raise DecisionError(400, f"Gate {gate} accepts: {', '.join(allowed)}.")

    if gate == 1:
        if not (story_text or "").strip():
            raise DecisionError(400, "Edit the story, then continue.")
        brief = conn.execute("select * from briefs where series_id = %s order by version desc limit 1", (series_id,)).fetchone()
        conn.execute("insert into briefs (series_id, version, keywords, aesthetic_note, story_text) values (%s, %s, %s, %s, %s)",
                     (series_id, brief["version"] + 1, brief["keywords"], brief["aesthetic_note"], story_text.strip()))
    elif gate == 2 and decision == "reject":
        if not notes:
            raise DecisionError(400, "Rejecting the story needs notes.")
        if target not in REJECT_TARGETS:
            raise DecisionError(400, f"Choose what to redo: {', '.join(REJECT_TARGETS)}.")
        audit.log(conn, None, f"note:reviewer_{target}", series_id, {"text": notes})
    elif gate == 3 and decision == "approve":
        speaking = [r["code"] for r in conn.execute("select code from characters where series_id = %s and voice_profile is not null order by code", (series_id,)).fetchall()]
        chosen = {k: v for k, v in (voices or {}).items() if str(v).strip()}
        missing = [c for c in speaking if c not in chosen]
        if missing:
            raise DecisionError(400, "Choose one voice for every speaking character. Missing: " + ", ".join(missing) + ".")
    elif gate == 3 and decision == "reject":
        ids = [i.strip() for i in target.split(",") if i.strip()]
        if not ids or not conn.execute("select 1 from character_assets where series_id = %s and id::text = any(%s) limit 1", (series_id, ids)).fetchone():
            raise DecisionError(400, "Rejecting the look needs the asset IDs to redo.")
        if notes:
            audit.log(conn, None, "note:reviewer_look", series_id, {"text": notes})

    payload = {"type": "gate", "gate": gate, "decision": decision, "notes": notes, "target": target}
    if gate == 3 and decision == "approve":
        payload["voices"] = {k: str(v) for k, v in (voices or {}).items()}
    _commit(conn, series_id, user_id, gate, decision, notes, payload, {"target": target} if target else None)
    return payload


def decide_pilot(conn, series_id, user_id, decision: str, notes: str = "", category: str = "") -> dict:
    """decide_pilot at Gate 4: continue to the full build, or discard (which needs notes and a category)."""
    _lock_gate(conn, series_id, 4)
    notes, category = (notes or "").strip(), (category or "").strip()
    if decision not in ("continue", "discard"):
        raise DecisionError(400, "Choose continue or discard.")
    if decision == "discard":
        if not notes:
            raise DecisionError(400, "Discarding needs notes.")
        if category not in DISCARD_CATEGORIES:
            raise DecisionError(400, f"Choose why: {', '.join(DISCARD_CATEGORIES)}.")
    payload = {"type": "gate", "gate": 4, "decision": decision, "notes": notes, "category": category}
    _commit(conn, series_id, user_id, 4, decision, notes, payload)
    return payload


def decide_feedback(conn, series_id, user_id, reassemble: list[int] | None = None) -> dict:
    """Send the confirmed feedback items to the graph (gate 4 or 5, decision 'feedback'), or recompile listed episodes ('reassemble').
    Gate 4 allows config key pilot_rework_rounds rounds of feedback; after that only continue or discard are offered."""
    from app import alerts, feedback
    from app.config import get_config
    from app.models.enums import AlertKind

    s = conn.execute("select * from series where id = %s for update", (series_id,)).fetchone()
    if s is None:
        raise DecisionError(404, "Production not found.")
    if s["status"] != "waiting_gate" or s["current_stage"] not in ("gate4", "gate5"):
        raise DecisionError(409, "Feedback can only be sent while the pilot or the final cut waits for your decision.")
    gate = int(s["current_stage"][4:])
    if reassemble:
        numbers = sorted({int(n) for n in reassemble})
        eps = conn.execute("select number, final_r2_key from episodes where series_id = %s and number = any(%s)", (series_id, numbers)).fetchall()
        if len(eps) != len(numbers) or any(not e["final_r2_key"] for e in eps):
            raise DecisionError(400, "Only compiled episodes can be reassembled.")
        ids = []
        for n in numbers:   # a recompile is a caption-category item with no shots: the compile step runs again and nothing is rendered
            row = conn.execute(
                "insert into feedback_items (series_id, episode_number, start_seconds, end_seconds, note, category, status, created_by) values (%s,%s,0,0,'Reassemble','caption','confirmed',%s) returning id",
                (series_id, n, user_id)).fetchone()
            ids.append(str(row["id"]))
        notes = "reassemble"
    else:
        if gate == 4:
            used = conn.execute("select count(*) n from gate_decisions where series_id = %s and gate = 4 and decision = 'feedback' and coalesce(notes, '') <> 'reassemble'", (series_id,)).fetchone()["n"]
            if used >= int(get_config("pilot_rework_rounds")):
                raise DecisionError(400, "The pilot rework rounds are used up. Continue to the full build or discard.")
        ids = feedback.confirmed_ids(conn, series_id)
        if not ids:
            raise DecisionError(400, "Confirm at least one feedback item first.")
        notes = ""
    payload = {"type": "gate", "gate": gate, "decision": "feedback", "notes": notes, "feedback_ids": ids}
    _commit(conn, series_id, user_id, gate, "feedback", notes, payload, {"items": len(ids)})
    alerts.notify([user_id], AlertKind.action_started, ("Recompile started." if reassemble else f"Rework started on {len(ids)} feedback item(s)."), f"/productions/{series_id}", series_id, conn=conn)
    return payload
