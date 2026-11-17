"""DRY_RUN fixtures for the story stages: schema-valid objects that pass the REAL validators.

The handlers hand these to llm.complete(fixture=...), so a dry run exercises the same validation and
persistence code as a real run. Text is in Bangla when the series language is Bangla.
"""
from __future__ import annotations

from app.config import get_config
from app.models.story import (BeatSheet, CharacterDef, ContinuityReport, EpisodeBeat, EpisodeScript, PlacementItem, PlacementPlan, PlotCheck, Scene, ScriptLine,
                              SceneRef, SetDef, StoryBible, WardrobeItemOut, WardrobePlan)

VISUAL_EN = ("A woman in her late twenties with shoulder-length dark hair, warm brown eyes and a calm, watchful expression, wearing a plain dark jacket "
             "over a cream blouse, soft natural makeup, an unadorned silver chain, composed posture, quiet confidence, understated styling and an even skin tone.")
WORDS_BN = "আমি জানি তুমি কী করেছ এবং আমি আর চুপ করে থাকব না আজ রাতে".split()
WORDS_EN = "I know what you did and I will not stay silent about it any longer tonight".split()
EMOTIONS = ["neutral", "angry", "sad"]


def _line_text(language: str, i: int, words: int = 12) -> str:
    base = WORDS_BN if language == "bn" else WORDS_EN
    return " ".join(base[(i + k) % len(base)] for k in range(words))


def make_bible(ctx: dict) -> StoryBible:
    roles = ctx["roles"] or [None]
    chars = [CharacterDef(code=f"CH{i:02d}", slot_role=r, name=f"Lead {i}", role=r or "lead", is_lead=True, age=28, personality="guarded",
                          visual_description=VISUAL_EN, speaks=True) for i, r in enumerate(roles, 1)]
    chars.append(CharacterDef(code=f"CH{len(chars) + 1:02d}", slot_role=None, name="Supporting", role="rival", is_lead=False, age=35, personality="proud",
                              visual_description=VISUAL_EN, speaks=True))
    sets = [SetDef(code=f"SET{i:02d}", name=f"Place {i}", description="A lived-in interior with soft practical light.",
                   times_of_day=["day", "night"] if i <= 2 else ["day"]) for i in range(1, 5)]
    return StoryBible(title="The Quiet Debt", logline="A woman returns to settle an old debt.", tone="tense", central_conflict="debt versus trust",
                      world_rules=["Everyone knows everyone."], characters=chars, sets=sets, episode_count=int(get_config("dryrun_episode_count")),
                      series_summary="A fixture story bible. " + "The arc escalates. " * 20)


def make_beats(ctx: dict, start: int, end: int) -> BeatSheet:
    codes = [c.code for c in ctx["bible"].characters]
    return BeatSheet(episodes=[EpisodeBeat(number=n, summary=f"Episode {n} moves the debt plot forward.", character_codes=codes, set_codes=["SET01"],
                                           hook="A letter arrives.", cliffhanger="The door opens.", handoff={c: "knows the secret" for c in codes}) for n in range(start, end + 1)])


def make_script(ctx: dict, number: int) -> EpisodeScript:
    """Fifteen lines in three scenes; three consecutive lines per speaker; about 80 s by the guide's estimate."""
    speakers = [c.code for c in ctx["bible"].characters if c.speaks]
    lines = [ScriptLine(character_code=speakers[(i // 3) % len(speakers)], text=_line_text(ctx["language"], i), emotion=EMOTIONS[i % 3]) for i in range(15)]
    scenes = [Scene(index=k, set_code="SET01", time_of_day="day", action="Two people face each other across a table.", lines=lines[k * 5:(k + 1) * 5]) for k in range(3)]
    return EpisodeScript(number=number, hook_action="A letter arrives.", scenes=scenes, cliffhanger_action="The door opens.", pronunciations=[])


def make_wardrobe(ctx: dict, scripts: dict) -> WardrobePlan:
    from app.stages.story import line_appearances

    app = line_appearances(scripts)
    return WardrobePlan(items=[WardrobeItemOut(character_code=c.code, code="W01", description="Dark jacket over a cream blouse.",
                                               scenes=[SceneRef(episode=e, scene=s) for e, s in sorted(app.get(c.code, set()))]) for c in ctx["bible"].characters])


def make_placement(ctx: dict, episodes: list[int]) -> PlacementPlan:
    allowed = ctx["brand"]["placement_types"] if ctx["brand"] else ["phone_full"]
    return PlacementPlan(placements=[PlacementItem(episode=n, kind=allowed[0], description="The lead checks the brand on a phone.") for n in (2, 5) if n in episodes])


def make_continuity() -> ContinuityReport:
    return ContinuityReport(edits=[])


def make_plot_check() -> PlotCheck:
    return PlotCheck(same_story=False, nearest_source_id=None, reason="The plot differs from every reference.")
