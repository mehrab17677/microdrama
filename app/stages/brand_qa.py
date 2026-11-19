"""Brand checks at episode level (guide 11.4, qa_brand): placement_count and exclusions."""
from __future__ import annotations

from pydantic import BaseModel

from app import db, placement
from app.stages import shots as shots_stage
from app.text import contains_term, normalize_for_match
from app.vendors import llm

INSTRUCTIONS = """You check one scene of a micro-drama against what the brand must never be shown next to.
You are given the scene's action and dialogue, how the brand appears in it, and the exclusion list.
Return violations: one entry for each exclusion that applies to this scene (the scene is about, shows or alludes to it), with a short reason.
Return an empty list when none applies. A mention of an exclusion in passing that has nothing to do with the scene does not count."""


class ExclusionViolation(BaseModel):
    exclusion: str
    reason: str


class ExclusionCheck(BaseModel):
    violations: list[ExclusionViolation]


def _scene_text(episode: dict, scene_index: int) -> str:
    for sc in (episode.get("script") or {}).get("scenes", []):
        if sc["index"] == scene_index:
            return sc["action"] + "\n" + "\n".join(f"{l['character_code']}: {l['text']}" for l in sc["lines"])
    return ""


def evaluate(series_id, episode: dict, fixture=None) -> tuple[list[tuple[str, bool, dict]], dict | None]:
    """([(check, passed, detail)], model usage). No placement in the episode: both checks pass trivially."""
    plan = db.fetch_all("select * from placements where series_id = %s and episode_number = %s order by created_at", (series_id, episode["number"]))
    brief = db.fetch_one("select * from brand_briefs where series_id = %s order by version desc limit 1", (series_id,))
    if not plan:
        return [("placement_count", True, {"planned": 0}), ("exclusions", True, {"planned": 0})], None
    cut = episode["cut_version"]
    in_cut = {r["shot_id"]: r for r in db.fetch_all("select e.shot_id, s.idx, s.spec from edit_entries e join shots s on s.id = e.shot_id where e.episode_id = %s and e.cut_version = %s", (episode["id"], cut))}
    spoken_ok = True
    wording = (brief or {}).get("spoken_wording")
    if wording:
        said = " ".join(normalize_for_match(r["text"]) for r in db.fetch_all("select text from dialogue_lines where episode_id = %s", (episode["id"],)))
        spoken_ok = contains_term(said, normalize_for_match(wording))
    missing, manual, scenes = [], [], {}
    for p in plan:
        row = in_cut.get(p["shot_id"]) if p["shot_id"] else None
        if row is None:
            missing.append({"placement": p["kind"], "why": "no shot in the latest cut carries it"})
            continue
        scenes[p["kind"]] = row["spec"].get("scene_index", 0)
        if p["kind"] in placement.FULL_FRAME and p["status"] not in ("placed", "verified"):
            missing.append({"placement": p["kind"], "why": f"the insert was not built (status {p['status']})"})
        elif p["kind"] == "spoken" and not spoken_ok:
            missing.append({"placement": p["kind"], "why": "the brand wording is not said in the dialogue"})
        elif p["kind"] in placement.MANUAL:
            manual.append(p["kind"])
    rows = [("placement_count", not missing, {"planned": len(plan), "in_cut": len(plan) - len(missing), "missing": missing, "manual_review": manual})]
    usage = None
    exclusions = list((brief or {}).get("exclusions") or [])
    violations: list[dict] = []
    for kind, scene in scenes.items():
        if not exclusions:
            break
        material = f"Exclusion list: {', '.join(exclusions)}\nBrand placement in this scene: {kind}\n\nScene {scene}:\n{_scene_text(episode, scene)}"
        out, u = llm.complete("writer", INSTRUCTIONS, [{"role": "user", "content": material}], schema=ExclusionCheck, fixture=fixture or (lambda: ExclusionCheck(violations=[])))
        usage = u
        violations += [{"placement": kind, "scene": scene, **v.model_dump()} for v in out.violations]
    rows.append(("exclusions", not violations, {"violations": violations, "checked": exclusions}))
    return rows, usage
