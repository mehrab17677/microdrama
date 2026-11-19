"""Burned-in captions (guide 10.7): an .ass file with one style, chunks of at most 4 words (bn) or 5 (en).

Caption text is the original line text: not the pronunciation respelling that was sent to the voice, and without the audio tag.
Timing: chunk start = shot start in the episode + the line's offset inside the shot audio (which already includes pad_head)
+ the first word's start; chunk end = the same base + the last word's end + 0.15 s. A line without word timings has its
chunks spread evenly over the line's duration. When the respelled text has a different word count, timings are mapped by position.
"""
from __future__ import annotations

CHUNK_WORDS = {"bn": 4, "en": 5}
FONT = {"bn": "Noto Sans Bengali", "en": "Noto Sans"}
END_HOLD = 0.15
HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},64,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,4,0,2,60,60,260,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def chunks(words: list[str], size: int) -> list[list[int]]:
    """Consecutive word index groups of at most `size`."""
    return [list(range(i, min(i + size, len(words)))) for i in range(0, len(words), size)]


def line_chunks(text: str, language: str, base: float, line_seconds: float, timings: list[dict] | None) -> list[tuple[float, float, str]]:
    """[(start, end, text)] for one line. `base` is where the line starts in the episode."""
    words = text.split()
    if not words:
        return []
    groups = chunks(words, CHUNK_WORDS.get(language, 5))
    out: list[tuple[float, float, str]] = []
    if timings:
        n, m = len(words), len(timings)
        pos = (lambda i: i) if n == m else (lambda i: min(m - 1, int(i * m / n)))
        for g in groups:
            first, last = timings[pos(g[0])], timings[pos(g[-1])]
            out.append((base + float(first["start"]), base + float(last["end"]) + END_HOLD, " ".join(words[i] for i in g)))
        # the 0.15 s hold must not run into the next chunk of the same line: no two captions overlap
        return [(st, min(en, out[k + 1][0]) if k + 1 < len(out) else en, tx) for k, (st, en, tx) in enumerate(out)]
    step = line_seconds / len(groups)   # no word timings: spread the chunks evenly
    for k, g in enumerate(groups):
        out.append((base + k * step, base + (k + 1) * step, " ".join(words[i] for i in g)))
    return out


def _ts(seconds: float) -> str:
    cs = max(0, round(seconds * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", " ")


def build_ass(language: str, events: list[tuple[float, float, str]]) -> str:
    out = [HEADER.format(font=FONT.get(language, "Noto Sans"))]
    for start, end, text in sorted(events):
        out.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},Default,,0,0,0,,{_escape(text)}\n")
    return "".join(out)


def episode_events(language: str, shots: list[dict], timing: dict) -> list[tuple[float, float, str]]:
    """shots: [{start, lines: [{text, duration, words}]}] in order; start is the shot's start in the episode."""
    events: list[tuple[float, float, str]] = []
    for shot in shots:
        offset = timing["pad_head"]
        for line in shot["lines"]:
            events += line_chunks(line["text"], language, shot["start"] + offset, line["duration"], line.get("words"))
            offset += line["duration"] + timing["line_gap"]
    return events
