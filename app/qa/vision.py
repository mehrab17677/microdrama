"""Frame extraction and the vision check (guide 11.3).

Role vision_qa (vision_qa_strong for hero shots). Inputs: four frames, the master portrait, the outfit image, the shot's
action text, and for speaking shots the line's emotion. Pass when severity != 'major' and the three required booleans are true.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from pydantic import BaseModel

from app.vendors import llm

FRACTIONS = (0.10, 0.35, 0.65, 0.90)
CATEGORIES = ("face", "outfit", "action", "acting", "artefact")

INSTRUCTIONS = """You check one shot of a vertical micro-drama. You are given four frames from the clip (at 10%, 35%, 65% and 90%),
then the character's master portrait, then (when given) the outfit reference image.
Answer: face_consistent (the person in the frames is the person in the master portrait in every frame), outfit_correct (they wear the outfit
shown or described), action_happens (the described action is visible), acting_matches_emotion (only for a speaking shot: the face and body
show the emotion; otherwise null), artefacts (short phrases for visible defects such as extra fingers, melted faces, flicker, text or logos),
severity ('none', 'minor' or 'major') and category (the most important problem: face, outfit, action, acting or artefact; null if none).
Be strict about identity and obvious defects; do not fail a shot for style."""


class VisionQA(BaseModel):
    face_consistent: bool
    outfit_correct: bool
    action_happens: bool
    acting_matches_emotion: bool | None
    artefacts: list[str]
    severity: str                # 'none' | 'minor' | 'major'
    category: str | None         # 'face' | 'outfit' | 'action' | 'acting' | 'artefact'


def passed(v: VisionQA) -> bool:
    return v.severity != "major" and v.face_consistent and v.outfit_correct and v.action_happens


def fixture_pass() -> VisionQA:
    return VisionQA(face_consistent=True, outfit_correct=True, action_happens=True, acting_matches_emotion=None, artefacts=[], severity="none", category=None)


def extract_frames(video: str | Path, duration: float, out_dir: str | Path, fractions=FRACTIONS) -> list[Path]:
    """One PNG per fraction of the clip."""
    out = []
    for i, f in enumerate(fractions):
        path = Path(out_dir) / f"frame{i}.png"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{duration * f:.3f}", "-i", str(video), "-frames:v", "1", str(path)], check=True)
        out.append(path)
    return out


def check(role: str, frames: list[Path], master: bytes | None, outfit: bytes | None, action: str, emotion: str | None, fixture=None) -> tuple[VisionQA, dict]:
    images = [p.read_bytes() for p in frames] + [b for b in (master, outfit) if b]
    text = f"Action in this shot: {action}\n" + (f"The character speaks with this emotion: {emotion}\n" if emotion else "This shot has no dialogue (acting_matches_emotion must be null).\n")
    text += f"The first {len(frames)} images are the frames in order; then the master portrait" + (", then the outfit image." if outfit else ".")
    return llm.complete(role, INSTRUCTIONS, [{"role": "user", "content": text}], schema=VisionQA, images=images, fixture=fixture or fixture_pass)
