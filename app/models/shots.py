"""Shot-stage schemas (guide 9.1 and 10). Field names are fixed. est_seconds is the one field added to
ShotSpec by guide section 10: the Lead Director's estimate for shots with no dialogue (2 to 10 s)."""
from __future__ import annotations

from pydantic import BaseModel


class ShotCharacter(BaseModel):
    code: str
    outfit_code: str
    position: str                # e.g. 'frame left, facing right'
    mouth_visible_speaking: bool


class CameraSetupDef(BaseModel):
    name: str
    shot_size: str
    angle: str
    movement: str
    composition: str
    best_for: str


class ShotSpec(BaseModel):
    idx: int
    shot_type: str               # non_speaking | speaking_closeup | speaking_wider | hero | static_insert
    scene_index: int
    set_code: str
    time_of_day: str
    camera_setup_name: str       # an existing library name, or the name of new_setup
    new_setup: CameraSetupDef | None
    characters: list[ShotCharacter]
    line_indexes: list[int]      # indexes into the episode's dialogue lines, in order
    action: str
    sounds: list[str]
    is_hook: bool
    is_cliffhanger: bool
    placement_kind: str | None
    est_seconds: float           # 2 to 10; used only for shots with no dialogue


class ShotList(BaseModel):
    episode_number: int
    shots: list[ShotSpec]


class ShotRequest(BaseModel):
    prompt: str                  # at most 1,500 characters
    camera_fixed: bool
