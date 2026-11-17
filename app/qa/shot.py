"""Shot-level checks that are plain code (guide 11.1 face_match, 11.4 clipping)."""
from __future__ import annotations

import re
import statistics
import subprocess
from pathlib import Path

from app.qa import face


def face_match(frames: list[Path], master_vector: list[float], floor: float) -> tuple[bool, float | None, dict]:
    """Median similarity of the frames' faces to the master portrait. No face found in any frame (back to camera,
    very wide shot) passes with detail {'no_face': True}."""
    sims = []
    for f in frames:
        vec = face.face_vector(f.read_bytes())
        if vec is not None:
            sims.append(face.similarity(vec, master_vector))
    if not sims:
        return True, None, {"no_face": True}
    median = statistics.median(sims)
    return median >= floor, median, {"frames_with_face": len(sims), "similarities": [round(s, 3) for s in sims]}


def max_volume_db(path: str | Path) -> float | None:
    """Peak level from ffmpeg volumedetect; None when the file has no audio."""
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path), "-vn", "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True)
    m = re.search(r"max_volume:\s*(-?[\d.]+|-inf) dB", proc.stderr)
    if not m or m.group(1) == "-inf":
        return None
    return float(m.group(1))


def clipping(path: str | Path, gain_db: float, limit_db: float) -> tuple[bool, float | None, dict]:
    """Passes when the (gain-adjusted) peak is at or below limit_db (-0.5). No audio passes."""
    peak = max_volume_db(path)
    if peak is None:
        return True, None, {"no_audio": True}
    effective = peak + gain_db
    return effective <= limit_db, effective, {"measured_db": peak, "gain_db": gain_db}
