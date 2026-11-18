"""Feedback by timestamp (guide 12.6).

Reference format: "Episode N (MM:SS-MM:SS)" followed by the note. Validate (the episode is compiled, start < end, end <= runtime),
map to the shots whose edit_entries overlap the range in the latest cut, classify with a model (role copy), let the person confirm,
then send every confirmed item to the graph with decision 'feedback'. The graph's apply_feedback node does the rework.
"""
from __future__ import annotations

import re

from pydantic import BaseModel

from app import audit, db
from app.jobs import ledger
from app.models.enums import JobKind, Vendor
from app.vendors import llm

REFERENCE = re.compile(r"^Episode ([0-9]+) [(]([0-9]{2}):([0-9]{2})-([0-9]{2}):([0-9]{2})[)]$")
CATEGORIES = ("picture", "lipsync", "voice", "sound", "music", "caption", "story", "brand")
STORY_DECLINE = "Story changes need a Gate 2 rejection or a new production."


class FeedbackError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def parse_reference(text: str) -> tuple[int, int, int]:
    """(episode, start seconds, end seconds) from 'Episode 5 (00:14-00:20)'."""
    m = REFERENCE.match((text or "").strip())
    if not m:
        raise FeedbackError(400, "Write the reference as: Episode 5 (00:14-00:20)")
    ep, a_m, a_s, b_m, b_s = (int(x) for x in m.groups())
    if a_s > 59 or b_s > 59:
        raise FeedbackError(400, "Seconds must be 00 to 59.")
    return ep, a_m * 60 + a_s, b_m * 60 + b_s


def split_entry(entry: str) -> tuple[str, str]:
    """'Episode 5 (00:14-00:20) the face changes' -> ('Episode 5 (00:14-00:20)', 'the face changes')."""
    entry = (entry or "").strip()
    end = entry.find(")")
    if end < 0:
        raise FeedbackError(400, "Write the reference as: Episode 5 (00:14-00:20) followed by your note.")
    return entry[: end + 1], entry[end + 1:].strip()


def map_shots(episode_id, cut_version: int, start: float, end: float) -> list:
    """Shots of the episode's latest cut whose time range overlaps (start, end)."""
    rows = db.fetch_all(
        "select e.shot_id from edit_entries e join shots s on s.id = e.shot_id where e.episode_id = %s and e.cut_version = %s"
        " and e.start_seconds < %s and e.end_seconds > %s order by s.idx", (episode_id, cut_version, end, start))
    return [r["shot_id"] for r in rows]


def create_item(conn, series_id, user_id, reference: str, note: str) -> dict:
    ep_no, start, end = parse_reference(reference)
    note = (note or "").strip()
    if not note:
        raise FeedbackError(400, "Write what is wrong.")
    ep = conn.execute("select * from episodes where series_id = %s and number = %s", (series_id, ep_no)).fetchone()
    if ep is None or not ep["final_r2_key"]:
        raise FeedbackError(400, f"Episode {ep_no} is not compiled yet.")
    if not start < end:
        raise FeedbackError(400, "The start must be before the end.")
    if end > float(ep["runtime_seconds"] or 0) + 1e-9:
        raise FeedbackError(400, f"Episode {ep_no} is only {float(ep['runtime_seconds']):.0f} seconds long.")
    shots = [r["shot_id"] for r in conn.execute(
        "select e.shot_id from edit_entries e join shots s on s.id = e.shot_id where e.episode_id = %s and e.cut_version = %s and e.start_seconds < %s and e.end_seconds > %s order by s.idx",
        (ep["id"], ep["cut_version"], end, start)).fetchall()]
    item = conn.execute(
        "insert into feedback_items (series_id, episode_number, start_seconds, end_seconds, note, shot_ids, created_by) values (%s,%s,%s,%s,%s,%s,%s) returning *",
        (series_id, ep_no, start, end, note, shots, user_id)).fetchone()
    ledger.enqueue(JobKind.llm_feedback_classify, series_id=series_id, episode_id=ep["id"], vendor=Vendor.openai, request={"item_id": str(item["id"])}, conn=conn)
    audit.log(conn, user_id, "feedback.create", series_id, {"item": str(item["id"]), "episode": ep_no, "start": start, "end": end})
    return item


def confirm_item(conn, series_id, item_id, user_id, category: str | None = None) -> dict:
    item = conn.execute("select * from feedback_items where id = %s and series_id = %s for update", (item_id, series_id)).fetchone()
    if item is None:
        raise FeedbackError(404, "Feedback item not found.")
    if item["status"] not in ("received", "confirmed"):
        raise FeedbackError(409, "This feedback item is already being worked on.")
    category = (category or item["category"] or "").strip()
    if category not in CATEGORIES:
        raise FeedbackError(400, "Choose a category: " + ", ".join(CATEGORIES) + ".")
    if not item["shot_ids"] and category != "story":
        raise FeedbackError(400, "That time range does not cover any shot.")
    conn.execute("update feedback_items set category = %s, status = 'confirmed' where id = %s", (category, item_id))
    audit.log(conn, user_id, "feedback.confirm", series_id, {"item": str(item_id), "category": category})
    return {**item, "category": category, "status": "confirmed"}


def confirmed_ids(conn, series_id) -> list[str]:
    return [str(r["id"]) for r in conn.execute("select id from feedback_items where series_id = %s and status = 'confirmed' order by created_at", (series_id,)).fetchall()]


# ---------------------------------------------------------------- classification (role copy)

CLASSIFY_INSTRUCTIONS = """You sort a reviewer's note about a short vertical drama episode into exactly one category:
picture (what is seen: face, look, motion, artefacts, wrong object), lipsync (the mouth does not match the speech), voice (the voice, accent, delivery or pronunciation),
sound (sound effects, ambience, levels), music (the music), caption (the burned-in subtitles: wording, timing, style), story (plot, dialogue meaning, characters' choices),
brand (a product or brand placement). Return only the category."""


class FeedbackCategory(BaseModel):
    category: str


_KEYWORDS = (("lipsync", ("lip", "mouth", "sync")), ("caption", ("caption", "subtitle", "text")), ("music", ("music", "score", "song")),
             ("voice", ("voice", "accent", "pronounc", "tone of voice")), ("sound", ("sound", "noise", "loud", "ambience", "volume")),
             ("brand", ("brand", "logo", "product", "phone screen")), ("story", ("plot", "story", "dialogue meaning", "why would")))


def keyword_category(note: str) -> str:
    low = note.lower()
    for cat, words in _KEYWORDS:
        if any(w in low for w in words):
            return cat
    return "picture"


def classify(note: str) -> tuple[str, dict]:
    out, usage = llm.complete("copy", CLASSIFY_INSTRUCTIONS, [{"role": "user", "content": note}], schema=FeedbackCategory, fixture=lambda: FeedbackCategory(category=keyword_category(note)))
    cat = out.category.strip().lower()
    return (cat if cat in CATEGORIES else "picture"), usage
