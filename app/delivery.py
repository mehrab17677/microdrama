"""What the brand and the team download when a production is done (guide 12.8 Delivery page): episodes, master, subtitles, marketing copy,
cost report, QA report and, for an ad placement drama, the placement report."""
from __future__ import annotations

import csv
import io

from app import db, placement
from app.jobs.handlers.delivery_handlers import marketing_key, master_key
from app.vendors import storage


def _media(series_id, key: str | None) -> str | None:
    prefix = f"series/{series_id}/"
    return f"/media/{series_id}/{key[len(prefix):]}" if key and key.startswith(prefix) else None


def _exists(key: str) -> bool:
    try:
        return any(k == key for k, _, _ in storage.list_objects(key))
    except Exception:  # noqa: BLE001 - a listing problem must not break the page
        return False


def files(series_id) -> dict:
    episodes = []
    for e in db.fetch_all("select number, final_r2_key, subtitle_r2_key, cut_version, runtime_seconds, status from episodes where series_id = %s order by number", (series_id,)):
        mk = marketing_key(series_id, e["number"])
        episodes.append({"number": e["number"], "status": e["status"], "cut": e["cut_version"], "runtime": float(e["runtime_seconds"] or 0),
                         "video": _media(series_id, e["final_r2_key"]), "subtitles": _media(series_id, e["subtitle_r2_key"]), "marketing": _media(series_id, mk) if _exists(mk) else None})
    master = master_key(series_id)
    return {"episodes": episodes, "master": _media(series_id, master) if _exists(master) else None,
            "has_placements": bool(db.fetch_one("select 1 from placements where series_id = %s limit 1", (series_id,)))}


def cost_report_csv(series_id) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["section", "vendor", "model", "unit", "units", "cost_usd"])
    total = 0.0
    for r in db.fetch_all("select vendor, model, unit_name, sum(units) units, sum(cost_usd) cost from cost_log where series_id = %s group by 1, 2, 3 order by 1, 2, 3", (series_id,)):
        w.writerow(["spend", r["vendor"], r["model"], r["unit_name"], f"{float(r['units']):.2f}", f"{float(r['cost']):.4f}"])
        total += float(r["cost"])
    for r in db.fetch_all("select j.episode_id, e.number, sum(c.cost_usd) cost from cost_log c join jobs j on j.id = c.job_id left join episodes e on e.id = j.episode_id where c.series_id = %s group by 1, 2 order by 2 nulls first", (series_id,)):
        w.writerow(["by_episode", "", f"episode {r['number']}" if r["number"] else "series-wide", "", "", f"{float(r['cost']):.4f}"])
    for b in db.fetch_all("select * from budgets where series_id = %s order by batch", (series_id,)):
        w.writerow(["video_budget", "higgsfield+fal", b["batch"], "usd", f"budget {float(b['budget_usd']):.2f} + approved {float(b['approved_extra_usd']):.2f}", f"spent {float(b['spent_usd']):.4f}"])
    w.writerow(["total", "", "", "", "", f"{total:.4f}"])
    w.writerow(["note", "", "", "", "", "video spend is an estimate until it is compared with the Higgsfield and fal invoices"])
    return buf.getvalue()


def qa_report_csv(series_id) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["episode", "shot", "round", "check", "passed", "category", "score", "note"])
    rows = db.fetch_all(
        "select e.number, s.idx, q.qa_round, q.check_name, q.passed, q.category, q.score, q.detail, q.created_at from qa_results q left join episodes e on e.id = q.episode_id"
        " left join shots s on s.id = q.shot_id where q.series_id = %s and q.qa_round in ('qa1','qa2') order by e.number nulls last, s.idx nulls first, q.check_name, q.created_at", (series_id,))
    latest: dict = {}
    for r in rows:   # the newest result per check (a repaired shot keeps only its last attempt)
        latest[(r["number"], r["idx"], r["check_name"])] = r
    for (number, idx, check), r in latest.items():
        note = "no lip-sync scorer: judged by people at the gates" if r["category"] == "unscored" else ""
        w.writerow([number if number is not None else "", idx if idx is not None else "", r["qa_round"], check, "pass" if r["passed"] else "FAIL", r["category"] or "",
                    f"{float(r['score']):.3f}" if r["score"] is not None else "", note])
    for s in db.fetch_all("select e.number, s.idx, s.status from shots s join episodes e on e.id = s.episode_id where s.series_id = %s and s.status in ('qa_failed','needs_human') order by 1, 2", (series_id,)):
        w.writerow([s["number"], s["idx"], "", "shot_status", "FAIL", s["status"], "", "needs a person"])
    return buf.getvalue()


def has_delivery(series_id) -> bool:
    return bool(db.fetch_one("select 1 from jobs where series_id = %s and kind = 'ffmpeg_master' and status = 'succeeded' limit 1", (series_id,)))
