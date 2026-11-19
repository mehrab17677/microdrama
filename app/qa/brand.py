"""The brand fidelity check (docs/AD_PLACEMENT.md): is the REAL product in the shot, unchanged, and shown the way the brief asks?

Role vision_qa_strong. Inputs: frames of the shot (or the keyframe), the real product photos, the logo, the printed text the pack must carry.
The model reports what it sees; the pass/fail decision, the label comparison and the exposure estimate are made in code.
"""
from __future__ import annotations

import difflib

from pydantic import BaseModel

from app.text import normalize_for_match
from app.vendors import llm

LABEL_MATCH_MIN = 0.75    # share of the expected printed lines that must be found in what the model read
LINE_SIMILARITY = 0.85    # one wrong letter in a long word still counts; a garbled word does not

INSTRUCTIONS = """You check a generated video shot (or its opening frame) against the REAL pictures of a product. You are given the frames first, in order, then
the real product photos, then the real logo. The product, its label and its logo in the real pictures are the truth.
Report what you see, strictly:
- product_visible_frames: the 0-based numbers of the frames in which the product is clearly visible.
- shape_matches and colours_match: the product in the frames has the same shape, proportions and colours as in the real photos.
- label_legible: the printed text on the product can be read in at least one frame.
- label_text_read: the text you can actually read on the product, exactly as it appears; leave empty if you cannot read it. Do not guess or complete words.
- logo_correct: true when a logo is visible and matches the real logo in shape and colours; false when it is visible but wrong, distorted or invented; null when no logo is visible.
- extra_or_competing_brands: another brand, logo or product label is visible.
- invented_text: text appears that is not on the real product and not part of the set (garbled letters, captions, watermarks).
- used_correctly: how the product is used matches what it is for; null when it is not being used.
- notes: one short sentence on the main problem, or empty. severity is none, minor or major (major = the product or its label is wrong, distorted or missing)."""


class BrandVision(BaseModel):
    product_visible_frames: list[int]
    shape_matches: bool
    colours_match: bool
    label_legible: bool
    label_text_read: str
    logo_correct: bool | None
    extra_or_competing_brands: bool
    invented_text: bool
    used_correctly: bool | None
    notes: str
    severity: str


def fixture_pass(expected: list[str], frames: int = 4, kind: str | None = None) -> BrandVision:
    return BrandVision(product_visible_frames=list(range(frames)), shape_matches=True, colours_match=True, label_legible=True, label_text_read=" ".join(expected),
                       logo_correct=True if kind == "logo_in_scene" else None, extra_or_competing_brands=False, invented_text=False, used_correctly=None, notes="", severity="none")


def label_score(read: str, expected: list[str]) -> float:
    """Share of the expected printed lines found (allowing small spelling slips) in the text the model read."""
    expected = [normalize_for_match(e) for e in expected if e.strip()]
    if not expected:
        return 1.0
    text = normalize_for_match(read)
    words = text.split()
    found = 0
    for line in expected:
        n = max(1, len(line.split()))
        windows = [" ".join(words[i:i + n]) for i in range(max(1, len(words) - n + 1))] or [""]
        if line in text or max(difflib.SequenceMatcher(None, line, w).ratio() for w in windows) >= LINE_SIMILARITY:
            found += 1
    return found / len(expected)


def judge(v: BrandVision, kind: str, expected: list[str], frame_count: int, planned: float) -> tuple[bool, float, str | None, dict]:
    """(passed, estimated seconds the product is visible, failure category, detail)."""
    visible = sorted({i for i in v.product_visible_frames if 0 <= i < frame_count})
    exposure = round(len(visible) / frame_count * planned, 2) if frame_count else 0.0
    score = label_score(v.label_text_read, expected) if kind == "product_shown" else None
    problems: list[str] = []
    if kind == "logo_in_scene":
        if v.logo_correct is not True:
            problems.append("the logo is missing, wrong or distorted")
    else:
        if not visible:
            problems.append("the product is not visible")
        if not v.shape_matches:
            problems.append("the product's shape does not match the real photos")
        if not v.colours_match:
            problems.append("the product's colours do not match the real photos")
        if kind == "product_shown":
            if not v.label_legible:
                problems.append("the label is not legible")
            elif score is not None and score < LABEL_MATCH_MIN:
                problems.append(f"the label text read does not match the pack ({score:.0%} of the printed lines found)")
        if kind == "product_in_use" and v.used_correctly is False:
            problems.append("the product is not used the way it is meant to be")
        if v.logo_correct is False:
            problems.append("the logo is distorted or wrong")
    if v.extra_or_competing_brands:
        problems.append("another brand or label appears")
    if v.invented_text:
        problems.append("invented or garbled text appears")
    if v.severity == "major" and not problems:
        problems.append(v.notes or "a major problem")
    detail = {**v.model_dump(), "visible_frames": visible, "exposure_seconds": exposure, "label_score": score, "problems": problems}
    return not problems, exposure, (None if not problems else "brand"), detail


def check(frames: list[bytes], product_refs: list[bytes], logo_ref: bytes | None, kind: str, expected: list[str], fixture=None) -> tuple[BrandVision, dict]:
    images = [*frames, *product_refs, *([logo_ref] if logo_ref else [])]
    text = (f"Placement type: {kind.replace('_', ' ')}. {len(frames)} frame(s), then {len(product_refs)} real product photo(s)" + (", then the real logo." if logo_ref else ".")
            + "\nThe real pack carries this printed text: " + " | ".join(expected))
    return llm.complete("vision_qa_strong", INSTRUCTIONS, [{"role": "user", "content": text}], schema=BrandVision, images=images,
                        fixture=fixture or (lambda: fixture_pass(expected, len(frames), kind)))
