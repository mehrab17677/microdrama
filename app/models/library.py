"""Schemas for source analysis (guide 9.3). Field names are fixed."""
from typing import Literal

from pydantic import BaseModel

PatternKind = Literal["trope", "beat_structure", "pacing", "hook", "cliffhanger", "archetype", "emotional_turn"]
TermKind = Literal["character", "place", "invented"]
PATTERN_KINDS = ("trope", "beat_structure", "pacing", "hook", "cliffhanger", "archetype", "emotional_turn")


class TermDef(BaseModel):
    latin: str      # the term in Latin script
    bangla: str     # the same term in Bangla script
    kind: TermKind


class GroupAnalysis(BaseModel):
    notes: str      # English notes on one part of the source
    terms: list[TermDef]


class PatternCardDef(BaseModel):
    kind: PatternKind
    title: str
    body: str       # English, at most 80 words, no names, no quoted text


class SourceCardDef(BaseModel):
    logline: str
    tone: str
    setting: str
    main_conflict: str
    themes: list[str]


class SourceAnalysis(BaseModel):
    pattern_cards: list[PatternCardDef]
    plot_summary: str   # about 200 words
    source_card: SourceCardDef
