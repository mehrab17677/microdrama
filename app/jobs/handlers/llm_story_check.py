"""llm_story_check (role writer): is a user-supplied story complete? (guide 12.4)"""
from __future__ import annotations

from app import db
from app.graphs import common
from app.models.story import StoryCheck
from app.vendors import llm

KINDS = ["llm_story_check"]

INSTRUCTIONS = """You check whether a story is complete enough to inspire a 45-episode micro-drama.
Answer three questions about the story: has_named_leads (at least one named lead character), has_conflict (a central
conflict), has_beginning_middle_end (a beginning, a middle and an ending). Be strict about the ending."""


def run(job: dict) -> dict:
    brief = common.latest_brief(job["series_id"])
    result, usage = llm.complete("writer", INSTRUCTIONS, [{"role": "user", "content": brief["story_text"] or ""}], schema=StoryCheck)
    return {**result.model_dump(), "actual_cost": usage["cost_usd"]}
