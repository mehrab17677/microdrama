"""Look-stage schemas (guide 9.1 and 9.5). Field names are fixed.

EmotionGroupsOut and LineTagsOut are what the model is asked for; they use lists of entries because a
free-form dict comes back empty from Claude's structured output.
"""
from __future__ import annotations

from pydantic import BaseModel


class VoiceProfile(BaseModel):
    voice_gender: str
    voice_age: str
    pitch: str
    pace: str
    accent: str                  # for 'bn' must state Bangladeshi
    texture: str
    energy: str
    speech_habits: str
    design_prompt: str           # one paragraph sent to voice design


class EmotionGroup(BaseModel):
    name: str                    # one lower-case word, e.g. 'anger'
    emotions: list[str]          # the free-text emotions it covers


class EmotionGroupsOut(BaseModel):
    groups: list[EmotionGroup]


class LineTag(BaseModel):
    idx: int                     # the line's index within the episode
    tag: str                     # one to three English words


class LineTagsOut(BaseModel):
    tags: list[LineTag]
