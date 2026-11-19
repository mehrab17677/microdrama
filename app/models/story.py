"""Story-stage schemas (guide 9.1). Field names are fixed.

The guide's shapes are what is validated and stored. Two of them contain free-form dicts
(EpisodeBeat.handoff, WardrobeItem.scenes). Claude's structured output accepts a free-form dict
but then returns it EMPTY, so the model is asked for a list of {code, state} / {episode, scene}
entries (the *Out classes) and the result is converted to the guide's shape.
"""
from __future__ import annotations

from pydantic import BaseModel


class StoryCheck(BaseModel):
    has_named_leads: bool
    has_conflict: bool
    has_beginning_middle_end: bool


class CharacterDef(BaseModel):
    code: str                    # 'CH01', 'CH02', ...
    slot_role: str | None        # the intake role label this character fills, else None
    name: str
    role: str                    # story function, e.g. 'female lead'
    is_lead: bool
    age: int
    personality: str
    visual_description: str      # 40 to 80 words, physical and styling only
    speaks: bool


class SetDef(BaseModel):
    code: str                    # 'SET01'
    name: str
    description: str
    times_of_day: list[str]      # subset of ['day', 'night', 'dusk']


class StoryBible(BaseModel):
    title: str
    logline: str
    tone: str
    central_conflict: str
    world_rules: list[str]
    characters: list[CharacterDef]
    sets: list[SetDef]           # 4 to 8 items
    episode_count: int           # 45 to 50
    series_summary: str          # about 300 words


class PlacementBeat(BaseModel):
    kind: str                    # product_in_use | product_shown | product_ambient | logo_in_scene | spoken
    description: str


class EpisodeBeat(BaseModel):
    number: int
    summary: str
    character_codes: list[str]
    set_codes: list[str]
    hook: str
    cliffhanger: str
    handoff: dict[str, str]      # character code -> what they know, want and hold at the end
    placement: PlacementBeat | None = None


class BeatSheet(BaseModel):
    episodes: list[EpisodeBeat]


class ScriptLine(BaseModel):
    character_code: str
    text: str                    # in the series language, at most 20 words
    emotion: str                 # free text


class Scene(BaseModel):
    index: int
    set_code: str
    time_of_day: str
    action: str
    lines: list[ScriptLine]


class Pronunciation(BaseModel):
    term: str
    spoken_as: str


class EpisodeScript(BaseModel):
    number: int
    hook_action: str
    scenes: list[Scene]
    cliffhanger_action: str
    pronunciations: list[Pronunciation]


class WardrobeItem(BaseModel):
    character_code: str
    code: str                    # 'W01'
    description: str
    scenes: list[dict]           # [{'episode': 3, 'scene': 2}]


# ---- shapes asked of the model ---------------------------------------------------------------

class HandoffEntry(BaseModel):
    code: str
    state: str


class EpisodeBeatOut(BaseModel):
    number: int
    summary: str
    character_codes: list[str]
    set_codes: list[str]
    hook: str
    cliffhanger: str
    handoff: list[HandoffEntry]
    placement: PlacementBeat | None = None

    def to_beat(self) -> EpisodeBeat:
        data = self.model_dump()
        data["handoff"] = {h["code"]: h["state"] for h in data["handoff"]}
        return EpisodeBeat.model_validate(data)


class BeatSheetOut(BaseModel):
    episodes: list[EpisodeBeatOut]

    def to_sheet(self) -> BeatSheet:
        return BeatSheet(episodes=[e.to_beat() for e in self.episodes])

    @classmethod
    def from_sheet(cls, sheet: BeatSheet) -> "BeatSheetOut":
        return cls(episodes=[EpisodeBeatOut(**{**e.model_dump(), "handoff": [HandoffEntry(code=k, state=v) for k, v in e.handoff.items()]}) for e in sheet.episodes])


class SceneRef(BaseModel):
    episode: int
    scene: int


class WardrobeItemOut(BaseModel):
    character_code: str
    code: str
    description: str
    scenes: list[SceneRef]

    def to_item(self) -> WardrobeItem:
        return WardrobeItem(character_code=self.character_code, code=self.code, description=self.description,
                            scenes=[s.model_dump() for s in self.scenes])


class WardrobePlan(BaseModel):
    items: list[WardrobeItemOut]


class PlacementItem(BaseModel):
    episode: int
    kind: str
    description: str


class PlacementPlan(BaseModel):
    placements: list[PlacementItem]


class ContinuityEdit(BaseModel):
    episode: int
    scene: int
    line_index: int
    new_text: str
    reason: str


class ContinuityReport(BaseModel):
    edits: list[ContinuityEdit]


class PlotCheck(BaseModel):
    same_story: bool
    nearest_source_id: str | None
    reason: str
