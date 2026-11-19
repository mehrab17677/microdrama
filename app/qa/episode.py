"""Episode-level checks (guide 11.4): runtime, hook, cliffhanger, loudness, continuity contact sheet, captions.
These are not auto-repaired; failures are listed at the gate."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw
from pydantic import BaseModel

from app.media.durations import RUNTIME_RANGE
from app.vendors import llm

TILE = (180, 320)
COLUMNS = 6


# ---------------------------------------------------------------- code checks

def runtime_check(planned: list[float]) -> tuple[bool, float, dict]:
    total = sum(planned)
    lo, hi = RUNTIME_RANGE
    return lo <= total <= hi, total, {"range": [lo, hi]}


def hook_check(first_spec: dict, first_planned: float, max_seconds: float) -> tuple[bool, float, dict]:
    ok = bool(first_spec.get("is_hook")) and first_planned <= max_seconds
    return ok, first_planned, {"is_hook": bool(first_spec.get("is_hook")), "max_seconds": max_seconds}


def cliffhanger_check(last_spec: dict) -> tuple[bool, None, dict]:
    return bool(last_spec.get("is_cliffhanger")), None, {}


def integrated_loudness(path: str | Path) -> float | None:
    """Integrated loudness in LUFS from ffmpeg's ebur128 summary; None when there is no audio."""
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn", "-af", "ebur128", "-f", "null", "-"], capture_output=True, text=True)
    m = re.findall(r"\bI:\s+(-?[\d.]+) LUFS", proc.stderr)
    return float(m[-1]) if m else None


def loudness_check(path: str | Path, target: float, tolerance: float) -> tuple[bool, float | None, dict]:
    value = integrated_loudness(path)
    if value is None:
        return False, None, {"no_audio": True}
    return abs(value - target) <= tolerance, value, {"target": target, "tolerance": tolerance}


def parse_ass(text: str) -> list[tuple[float, float, str]]:
    def seconds(ts: str) -> float:
        h, m, s = ts.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)

    out = []
    for line in text.splitlines():
        if line.startswith("Dialogue:"):
            parts = line.split(",", 9)
            out.append((seconds(parts[1]), seconds(parts[2]), parts[9]))
    return out


def captions_check(ass_text: str, line_texts: list[str]) -> tuple[bool, None, dict]:
    """Every line has its caption chunks (the words of all chunks, in order, equal the words of all lines) and no two chunks overlap."""
    events = sorted(parse_ass(ass_text))
    said = " ".join(t for _, _, t in events).split()
    expected = " ".join(line_texts).replace("{", "(").replace("}", ")").split()
    overlaps = [(round(a[1], 2), round(b[0], 2)) for a, b in zip(events, events[1:]) if a[1] > b[0] + 1e-6]
    missing = said != expected
    return not (overlaps or missing), None, {"chunks": len(events), "overlaps": overlaps[:5], "words_missing_or_extra": missing}


# ---------------------------------------------------------------- continuity (vision)

class ContinuityIssue(BaseModel):
    shot_idx: int
    issue: str
    severity: str          # 'minor' | 'major'


class ContinuityQA(BaseModel):
    issues: list[ContinuityIssue]


CONTINUITY_INSTRUCTIONS = """You check the continuity of one micro-drama episode from a contact sheet: one frame per shot, left to right, top to bottom, each labelled with its shot number.
Report problems where the same person changes face, hair, outfit or age, where a place changes without a reason, or where props jump between shots.
Return issues as a list of {shot_idx, issue, severity} where severity is 'minor' or 'major'. Return an empty list when there is no problem. Differences of angle and lighting are not problems."""


def contact_sheet(frames: list[tuple[int, Path]]) -> bytes:
    """One frame per shot tiled into a grid, each labelled with its shot index (an internal review image)."""
    import io

    rows = -(-len(frames) // COLUMNS)
    sheet = Image.new("RGB", (COLUMNS * TILE[0], rows * TILE[1]), (20, 20, 20))
    draw = ImageDraw.Draw(sheet)
    for n, (idx, path) in enumerate(frames):
        x, y = (n % COLUMNS) * TILE[0], (n // COLUMNS) * TILE[1]
        sheet.paste(Image.open(path).convert("RGB").resize(TILE), (x, y))
        draw.rectangle((x, y, x + 46, y + 22), fill=(0, 0, 0))
        draw.text((x + 4, y + 5), f"#{idx}", fill=(255, 255, 255))
    out = io.BytesIO()
    sheet.save(out, format="PNG")
    return out.getvalue()


def continuity_check(sheet_png: bytes | None, fixture=None) -> tuple[bool, list[dict], dict]:
    result, usage = llm.complete("vision_qa", CONTINUITY_INSTRUCTIONS, [{"role": "user", "content": "The contact sheet is attached."}], schema=ContinuityQA,
                                 images=[sheet_png] if sheet_png else None, fixture=fixture or (lambda: ContinuityQA(issues=[])))
    issues = [i.model_dump() for i in result.issues]
    return not any(i["severity"] == "major" for i in issues), issues, usage
