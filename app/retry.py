"""Retrying a failed production from the step that failed (web side: no graph imports here).

A step that exhausts its automatic attempts (config max_attempts) stops the production: series status `failed`, with an audit row `series.failed`
naming the step and the reason. A person reads the failure on the production page and presses Retry. That:
  1. records `note:retry:<step>`, which (a) gives the step max_attempts more tries per retry (see steps.enqueue_step) and
     (b) restarts the redo counters of the check steps (see common.retry_since);
  2. sets the series back to running and queues a graph command of kind 'retry' with the node to restart from; the runner enters the graph there.
"""
from __future__ import annotations

from app import alerts, audit
from app.models.enums import AlertKind

# Steps built with add_step in the production graph: restart at enq_<step>, which enqueues exactly the work still missing.
ENQ_STEPS = ["story_ingest", "bible", "beats", "placement", "scripts", "continuity", "story_checks", "traits", "masters", "emotion_groups", "packs",
             "shot_lists", "audio_tags", "tts", "keyframes", "requests", "clips", "lipsync", "sound", "compile", "qa", "learn", "delivery"]
# Check nodes that can stop the production by themselves: restart at the node.
CHECK_NODES = ["intake_check", "validate_beats", "validate_shots", "fit_durations"]
STEP_LABELS = {"story_ingest": "Reading your story", "bible": "Story bible", "beats": "Episode beats", "placement": "Brand placement plan", "scripts": "Episode scripts",
               "continuity": "Continuity check", "story_checks": "Wardrobe and originality checks", "traits": "Character traits", "masters": "Master portraits",
               "emotion_groups": "Emotion groups", "packs": "Character sheets, set plates and voices", "shot_lists": "Shot lists", "audio_tags": "Line tags", "tts": "Voice lines",
               "keyframes": "Keyframes", "requests": "Video requests", "clips": "Video clips", "lipsync": "Lip-sync", "sound": "Sound", "compile": "Episode edit",
               "qa": "Quality checks", "learn": "Learning", "delivery": "Delivery", "intake_check": "Intake check", "validate_beats": "Beat sheet check",
               "validate_shots": "Shot list check", "fit_durations": "Timing check", "graph": "The production engine"}


def step_label(step: str) -> str:
    return STEP_LABELS.get(step, step.replace("_", " ").capitalize())


def retry_node(step: str) -> str | None:
    """The graph node to restart at, or None to continue from the last checkpoint (the step name `graph` means the engine itself stopped)."""
    if step in ENQ_STEPS:
        return f"enq_{step}"
    if step in CHECK_NODES:
        return step
    return None


def failure_info(conn_or_db, series_id) -> dict | None:
    """What the last failure said: {step, message}, from the series.failed audit row."""
    from app import db

    row = db.fetch_one("select detail from audit_log where series_id = %s and action = 'series.failed' order by created_at desc, id desc limit 1", (series_id,))
    return {"step": row["detail"].get("step", ""), "message": row["detail"].get("message", "")} if row else None


def failed_jobs(series_id, limit: int = 8) -> list[dict]:
    """The latest failed jobs (what failed, which attempt, why), newest first, for the person deciding whether to retry."""
    from app import db

    return [dict(r) for r in db.fetch_all(
        "select kind, attempt, error, coalesce(finished_at, created_at) at from jobs where series_id = %s and status = 'failed' order by coalesce(finished_at, created_at) desc limit %s",
        (series_id, limit))]


class RetryError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def request_retry(conn, series_id, user_id) -> dict:
    """One transaction: check the production is failed, note the retry, set it running and queue the restart command."""
    from psycopg.types.json import Jsonb

    s = conn.execute("select * from series where id = %s for update", (series_id,)).fetchone()
    if s is None:
        raise RetryError(404, "Production not found.")
    if s["status"] != "failed":
        raise RetryError(409, "Only a production that stopped with a failure can be retried.")
    row = conn.execute("select detail from audit_log where series_id = %s and action = 'series.failed' order by created_at desc, id desc limit 1", (series_id,)).fetchone()
    step = (row["detail"].get("step") if row else "") or "graph"
    node = retry_node(step)
    audit.log(conn, user_id, f"note:retry:{step}", series_id, {"text": "retry requested"})
    conn.execute("update series set status = 'running', waiting_group = null, updated_at = now() where id = %s", (series_id,))
    conn.execute(
        "insert into graph_commands (series_id, graph, thread_id, kind, payload, created_at) values (%s, 'production', %s, 'retry', %s, clock_timestamp())",
        (series_id, s["thread_id"], Jsonb({"type": "retry", "step": step, "node": node})))
    audit.log(conn, user_id, "series.retry", series_id, {"step": step, "node": node})
    alerts.notify([s["owner_id"]], AlertKind.action_started, f"Retrying {step_label(step).lower()}.", f"/productions/{series_id}", series_id, conn=conn)
    return {"step": step, "node": node}
