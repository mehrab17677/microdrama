"""Originality checks (guide 9.4), run by the llm_originality handler.

 Blocklist   normalise every script line and beat and search each blocklist term: any hit fails.
 Wording     any run of 8 identical consecutive words between a script and a source chunk fails that scene
             (same language only).
 Similarity  embed each scene; cosine similarity to the nearest source chunk above originality_cosine_max
             fails that scene (same language only).
 Plot        the model answers {same_story, nearest_source_id, reason} for the whole beat sheet.
Failed scenes are rewritten by ONE more script attempt for that episode. same_story = true is reported at
Gate 2 and not auto-fixed. The report is stored as a qa_results row with check_name = 'originality'.
"""
from __future__ import annotations

from typing import Callable

from psycopg.types.json import Jsonb

from app import db
from app.config import get_config
from app.models.story import EpisodeBeat, EpisodeScript, PlotCheck
from app.stages import story
from app.text import contains_term, normalize_for_match
from app.vendors import images, llm

WINDOW = 8
PLOT_INSTRUCTIONS = """You compare a new micro-drama's beat sheet with the plot summaries of existing stories.
Answer whether it is substantially the SAME STORY as any of them: the same sequence of events, the same character roles and
relationships, and the same twists, even with new names. Similar tropes or genre are fine and are not "the same story".
Return same_story, nearest_source_id (the id of the closest summary, or null) and a one-sentence reason."""


def scene_text(scene) -> str:
    return " ".join([scene.action, *[l.text for l in scene.lines]])


def _source_filter(ctx, same_language: bool = True) -> tuple[str, list]:
    """Sources the series is compared with: its genre at the pinned lore version, plus the user's own story.
    Wording and similarity only make sense within one language; the plot comparison does not."""
    s = ctx["series"]
    sql = ("src.status = 'ready' and " + ("src.language = %s and " if same_language else "")
           + "((src.series_id is null and src.genre_id = %s and (src.lore_version <= %s or %s is null)) or src.series_id = %s)")
    params = [ctx["language"]] if same_language else []
    return sql, [*params, s["genre_id"], s["pinned_lore_version"], s["pinned_lore_version"], s["id"]]


def check_blocklist(terms: list[str], scripts: dict[int, EpisodeScript], beats: dict[int, EpisodeBeat]) -> tuple[dict, dict]:
    """Returns ({(episode, scene): [terms]}, {episode: [terms]})."""
    scene_hits: dict = {}
    for ep, sc in scripts.items():
        for scene in sc.scenes:
            text = normalize_for_match(" . ".join([scene.action, *[l.text for l in scene.lines], *( [sc.hook_action] if scene.index == sc.scenes[0].index else []),
                                                   *( [sc.cliffhanger_action] if scene.index == sc.scenes[-1].index else [])]))
            hit = sorted(t for t in terms if contains_term(text, t))
            if hit:
                scene_hits[(ep, scene.index)] = hit
    beat_hits = {}
    for ep, b in beats.items():
        hit = story.blocklist_hits(b.model_dump(), terms)
        if hit:
            beat_hits[ep] = hit
    return scene_hits, beat_hits


def check_wording(ctx, scripts: dict[int, EpisodeScript]) -> dict:
    """{(episode, scene): [source ids]} for any scene sharing a run of 8 identical words with a source chunk.
    The corpus is streamed once; the scene n-grams are held in a dict."""
    grams: dict[tuple, list] = {}
    for ep, sc in scripts.items():
        for scene in sc.scenes:
            words = normalize_for_match(scene_text(scene)).split()
            for i in range(len(words) - WINDOW + 1):
                grams.setdefault(tuple(words[i:i + WINDOW]), []).append((ep, scene.index))
    hits: dict = {}
    if not grams:
        return hits
    where, params = _source_filter(ctx)
    with db.transaction() as conn:
        cur = conn.cursor(name="originality_scan")
        cur.itersize = 100
        cur.execute(f"select sc.text, src.id source_id from source_chunks sc join sources src on src.id = sc.source_id where {where}", params)
        for row in cur:
            words = normalize_for_match(row["text"]).split()
            for i in range(len(words) - WINDOW + 1):
                for key in grams.get(tuple(words[i:i + WINDOW]), ()):
                    hits.setdefault(key, set()).add(str(row["source_id"]))
    return {k: sorted(v) for k, v in hits.items()}


def check_similarity(ctx, scripts: dict[int, EpisodeScript]) -> tuple[dict, list[dict]]:
    """Returns ({(episode, scene): (similarity, source id)} above the limit, usages)."""
    limit = float(get_config("originality_cosine_max"))
    keys, texts = [], []
    for ep, sc in scripts.items():
        for scene in sc.scenes:
            keys.append((ep, scene.index))
            texts.append(scene_text(scene))
    if not texts:
        return {}, []
    where, params = _source_filter(ctx)
    vectors, usages = [], []
    for i in range(0, len(texts), 100):
        vecs, u = images.embed(texts[i:i + 100])
        vectors += vecs
        usages.append(u)
    out = {}
    for key, vec in zip(keys, vectors):
        row = db.fetch_one(
            f"select 1 - (sc.embedding <=> %s::vector) sim, src.id source_id from source_chunks sc join sources src on src.id = sc.source_id"
            f" where {where} and sc.embedding is not null order by sc.embedding <=> %s::vector limit 1", [db.to_vector(vec), *params, db.to_vector(vec)],
        )
        if row and row["sim"] is not None and float(row["sim"]) > limit:
            out[key] = (float(row["sim"]), str(row["source_id"]))
    return out, usages


def plot_check(ctx, beats: dict[int, EpisodeBeat]) -> tuple[PlotCheck, list[dict]]:
    where, params = _source_filter(ctx, same_language=False)   # plot summaries are English: a translated copy is still a copy
    summary = ctx["bible"].series_summary
    digest = " ".join(b.summary for b in beats.values())[:3000]
    (vec,), eu = images.embed([summary + " " + digest])
    sources = db.fetch_all(
        f"select src.id, src.plot_summary, min(sc.embedding <=> %s::vector) d from source_chunks sc join sources src on src.id = sc.source_id"
        f" where {where} and sc.embedding is not null and src.plot_summary is not null group by src.id, src.plot_summary order by d limit 5", [db.to_vector(vec), *params],
    )
    if not sources:
        return PlotCheck(same_story=False, nearest_source_id=None, reason="There is no reference to compare with."), [eu]
    material = ("## New series: beat sheet\n" + "\n".join(f"{n}. {b.summary}" for n, b in sorted(beats.items()))
                + "\n\n## Existing stories\n" + "\n\n".join(f"id {s['id']}:\n{s['plot_summary']}" for s in sources))
    result, usage = llm.complete("originality", PLOT_INSTRUCTIONS, [{"role": "user", "content": material}], schema=PlotCheck, fixture=lambda: __import__("app.stages.story_fixtures", fromlist=["x"]).make_plot_check())
    return result, [eu, usage]


def evaluate(ctx, terms, scripts, beats) -> tuple[dict, list[dict]]:
    """All scene-level checks. Returns ({'blocklist', 'beat_blocklist', 'wording', 'similarity'}, usages)."""
    scene_hits, beat_hits = check_blocklist(terms, scripts, beats)
    wording = check_wording(ctx, scripts)
    similarity, usages = check_similarity(ctx, scripts)
    return {"blocklist": scene_hits, "beat_blocklist": beat_hits, "wording": wording, "similarity": similarity}, usages


def failing_scenes(found: dict) -> dict[int, list[str]]:
    """episode -> reasons, for every scene that failed a check."""
    out: dict[int, list[str]] = {}
    for (ep, scene), hit in found["blocklist"].items():
        out.setdefault(ep, []).append(f"scene {scene} uses names or terms that belong to reference material ({', '.join(hit[:5])})")
    for (ep, scene) in found["wording"]:
        out.setdefault(ep, []).append(f"scene {scene} repeats 8 or more words in a row from reference material")
    for (ep, scene), (sim, _) in found["similarity"].items():
        out.setdefault(ep, []).append(f"scene {scene} is too similar to reference material (similarity {sim:.2f})")
    return out


def _flat(found: dict) -> dict:
    return {
        "blocklist": [{"episode": e, "scene": s, "terms": t} for (e, s), t in sorted(found["blocklist"].items())] + [{"episode": e, "scene": None, "terms": t} for e, t in sorted(found["beat_blocklist"].items())],
        "wording": [{"episode": e, "scene": s, "sources": src} for (e, s), src in sorted(found["wording"].items())],
        "similarity": [{"episode": e, "scene": s, "similarity": round(v[0], 3), "source": v[1]} for (e, s), v in sorted(found["similarity"].items())],
    }


def run(ctx, rewrite_episode: Callable[[int, list[str]], EpisodeScript | None], load_beats: Callable[[], dict]) -> tuple[dict, list[dict]]:
    """Run every check; rewrite failing episodes once; re-check them; store the report. Returns (report, usages)."""
    sid = ctx["id"]
    terms = story.blocklist(sid)
    scripts, beats = story.load_scripts(sid), load_beats()
    found, usages = evaluate(ctx, terms, scripts, beats)
    rewritten: list[int] = []
    for ep, reasons in sorted(failing_scenes(found).items()):
        new = rewrite_episode(ep, reasons)
        if new is not None:
            scripts[ep] = new
            rewritten.append(ep)
    if rewritten:
        found, u2 = evaluate(ctx, terms, scripts, beats)
        usages += u2
    plot, plot_usages = plot_check(ctx, beats)
    usages += plot_usages
    flat = _flat(found)
    passed = not (flat["blocklist"] or flat["wording"] or flat["similarity"]) and not plot.same_story
    report = {**flat, "rewritten_episodes": rewritten, "same_story": plot.same_story, "nearest_source_id": plot.nearest_source_id, "plot_reason": plot.reason, "passed": passed}
    with db.transaction() as conn:
        conn.execute("delete from qa_results where series_id = %s and check_name = 'originality'", (sid,))
        conn.execute("insert into qa_results (series_id, qa_round, check_name, passed, detail) values (%s, 'precheck', 'originality', %s, %s)", (sid, passed, Jsonb(report)))
    return report, usages
