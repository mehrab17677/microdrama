"""Code checks that the graph's check nodes run (guide 8.5 and 9). Each returns a list of problems."""
from __future__ import annotations

from app import db
from app.config import get_config


def validate_beat_rows(series_id) -> list[str]:
    """Numbers run 1..episode_count with no gaps, every code exists, hook and cliffhanger are non-empty."""
    problems: list[str] = []
    series = db.fetch_one("select episode_count from series where id = %s", (series_id,))
    eps = db.fetch_all("select number, beat from episodes where series_id = %s order by number", (series_id,))
    expected = series["episode_count"] or 0
    if not eps:
        return ["the beat sheet has no episodes"]
    numbers = [e["number"] for e in eps]
    if numbers != list(range(1, len(eps) + 1)):
        problems.append(f"episode numbers must run 1 to {len(eps)} with no gaps; got {numbers[:6]}...")
    if expected and len(eps) != expected:
        problems.append(f"the bible says {expected} episodes but the beat sheet has {len(eps)}")
    chars = {r["code"] for r in db.fetch_all("select code from characters where series_id = %s", (series_id,))}
    sets = {r["code"] for r in db.fetch_all("select code from sets where series_id = %s", (series_id,))}
    for e in eps:
        b = e["beat"]
        n = e["number"]
        if not str(b.get("hook", "")).strip():
            problems.append(f"episode {n} has no hook")
        if not str(b.get("cliffhanger", "")).strip():
            problems.append(f"episode {n} has no cliffhanger")
        for code in b.get("character_codes", []):
            if code not in chars:
                problems.append(f"episode {n} uses unknown character {code}")
        for code in b.get("set_codes", []):
            if code not in sets:
                problems.append(f"episode {n} uses unknown set {code}")
    return problems


def validate_shot_rows(episode_id) -> list[str]:
    """8 to 12 shots; first is the hook and last the cliffhanger; every dialogue line used exactly once
    and in order; a speaking shot has exactly one visibly speaking character whose lines they are."""
    lo, hi = get_config("shot_count_range")
    shots = db.fetch_all("select idx, shot_type, spec from shots where episode_id = %s order by idx", (episode_id,))
    problems: list[str] = []
    if not lo <= len(shots) <= hi:
        problems.append(f"an episode needs {lo} to {hi} shots, not {len(shots)}")
    if not shots:
        return problems
    if not shots[0]["spec"].get("is_hook"):
        problems.append("the first shot must be the hook")
    if not shots[-1]["spec"].get("is_cliffhanger"):
        problems.append("the last shot must be the cliffhanger")
    line_count = db.fetch_one("select count(*) n from dialogue_lines where episode_id = %s", (episode_id,))["n"]
    used = [i for s in shots for i in s["spec"].get("line_indexes", [])]
    if used != list(range(line_count)):
        problems.append(f"every dialogue line must be used once and in order (lines 0..{line_count - 1}); got {used}")
    for s in shots:
        spec = s["spec"]
        if spec.get("line_indexes"):
            speakers = [c for c in spec.get("characters", []) if c.get("mouth_visible_speaking")]
            if len(speakers) != 1:
                problems.append(f"shot {s['idx']} must have exactly one visibly speaking character, not {len(speakers)}")
    return problems
