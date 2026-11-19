"""Ad placement bookkeeping (docs/AD_PLACEMENT.md): linking placements to shots, status, human approval and the report.

A placement is a planned appearance of the brand INSIDE a generated scene. Statuses:
  planned   a shot carries it           placed    its clip was rendered and checked    verified   the machine checks passed
  failed    a machine check failed      approved  a person signed it off              rejected   a person rejected it (a brand feedback item reworks it)
"""
from __future__ import annotations

import csv
import io

from psycopg.types.json import Jsonb

from app import audit, db

VISUAL = ("product_in_use", "product_shown", "product_ambient", "logo_in_scene")
NEEDS_LABEL = ("product_shown",)                 # the label must be legible and read correctly
NEEDS_PRODUCT = ("product_in_use", "product_shown", "product_ambient")
KIND_LABEL = {"product_in_use": "Product in use", "product_shown": "Product shown, label to camera", "product_ambient": "Product in the scene",
              "logo_in_scene": "Logo in the scene", "spoken": "Spoken mention"}


def mmss(t: float) -> str:
    t = int(round(t))
    return f"{t // 60:02d}:{t % 60:02d}"


def asset_rows(series_id, kinds: tuple[str, ...] = ("product", "app_screen")) -> list[dict]:
    return db.fetch_all("select * from brand_assets where series_id = %s and kind = any(%s) order by (view = 'front') desc, created_at, id", (series_id, list(kinds)))


def reference_assets(series_id, kind: str, limit: int = 3) -> list[dict]:
    """The real pictures handed to the models for a placement: the product photos (front first, then other views) or the logo."""
    if kind == "logo_in_scene":
        return asset_rows(series_id, ("logo",))[:1] + asset_rows(series_id, ("product", "app_screen"))[:1]
    return (asset_rows(series_id, ("product", "app_screen"))[:limit - 1] + asset_rows(series_id, ("logo",))[:1])[:limit]


def brief(series_id) -> dict | None:
    return db.fetch_one("select * from brand_briefs where series_id = %s order by version desc limit 1", (series_id,))


# ---------------------------------------------------------------- linking and status

def link_shots(series_id, episode_number: int) -> None:
    """After a shot list is stored: tie each planned placement to the first shot of its kind."""
    ep = db.fetch_one("select id from episodes where series_id = %s and number = %s", (series_id, episode_number))
    for p in db.fetch_all("select id, kind from placements where series_id = %s and episode_number = %s", (series_id, episode_number)):
        shot = db.fetch_one("select id from shots where episode_id = %s and spec->>'placement_kind' = %s order by idx limit 1", (ep["id"], p["kind"]))
        db.execute("update placements set shot_id = %s, status = 'planned', approved_by = null, approved_at = null, review_note = null, exposure_seconds = null, fidelity = null where id = %s",
                   (shot and shot["id"], p["id"]))


def reset_for_shot(conn, shot_id) -> None:
    """The shot is being rendered again (repair or rework): its placement goes back to planned and loses any approval."""
    conn.execute("update placements set status = 'planned', approved_by = null, approved_at = null, exposure_seconds = null, fidelity = null where shot_id = %s", (shot_id,))


def record_check(conn, series_id, shot_id, passed: bool, exposure: float | None, fidelity: dict) -> None:
    """Called by qa_shot after the brand_fidelity check of a placement shot."""
    conn.execute("update placements set status = case when status in ('approved','rejected') then status else 'placed' end, exposure_seconds = %s, fidelity = %s"
                 " where series_id = %s and shot_id = %s", (exposure, Jsonb(fidelity), series_id, shot_id))


def recheck_exposure(series_id, batch: str) -> None:
    """qa_brand and qa_shot run at the same time, so the exposure of a placement may not have been written when qa_brand looked. After all QA jobs are done
    the exposure check is evaluated again from the stored seconds and its row replaced."""
    minimum = float((brief(series_id) or {}).get("min_exposure_seconds") or 3)
    rnd = "qa1" if batch == "pilot" else "qa2"
    for ep in db.fetch_all("select id, number from episodes where series_id = %s and batch = %s", (series_id, batch)):
        rows = db.fetch_all("select kind, exposure_seconds from placements where series_id = %s and episode_number = %s and kind = any(%s)", (series_id, ep["number"], list(VISUAL)))
        if not rows:
            continue
        short = [{"placement": r["kind"], "seconds": float(r["exposure_seconds"]), "minimum": minimum} for r in rows
                 if r["exposure_seconds"] is not None and float(r["exposure_seconds"]) < minimum and r["kind"] != "product_ambient"]
        with db.transaction() as conn:
            conn.execute("delete from qa_results where episode_id = %s and shot_id is null and qa_round = %s and check_name = 'exposure'", (ep["id"], rnd))
            conn.execute("insert into qa_results (series_id, episode_id, qa_round, check_name, passed, detail) values (%s,%s,%s,'exposure',%s,%s)",
                         (series_id, ep["id"], rnd, not short, Jsonb({"minimum_seconds": minimum, "short": short})))


def finalize(series_id, batch: str) -> dict:
    """After QA: a placement whose shot check and episode checks passed is 'verified', otherwise 'failed'. A person's approval or rejection is kept."""
    recheck_exposure(series_id, batch)
    counts = {"verified": 0, "failed": 0}
    for p in db.fetch_all("select p.* from placements p join episodes e on e.series_id = p.series_id and e.number = p.episode_number where p.series_id = %s and e.batch = %s", (series_id, batch)):
        if p["status"] in ("approved", "rejected"):
            continue
        ep = db.fetch_one("select id from episodes where series_id = %s and number = %s", (series_id, p["episode_number"]))
        latest: dict[str, bool] = {}
        for c in db.fetch_all("select check_name, passed from qa_results where episode_id = %s and shot_id is null and check_name in ('placement_count','exposure','exclusions','claims')"
                              " and qa_round in ('qa1','qa2') order by created_at desc", (ep["id"],)):
            latest.setdefault(c["check_name"], c["passed"])
        shot_ok = True
        if p["kind"] in VISUAL:
            rows = db.fetch_all("select passed, detail from qa_results where shot_id = %s and check_name = 'brand_fidelity' and qa_round in ('qa1','qa2')", (p["shot_id"],)) if p["shot_id"] else []
            shot_ok = bool(rows) and max(rows, key=lambda r: (int(r["detail"].get("attempts", 0)), int(r["detail"].get("sync_attempts", 0))))["passed"]
        ok = shot_ok and bool(latest) and all(latest.values())
        db.execute("update placements set status = %s where id = %s", ("verified" if ok else "failed", p["id"]))
        counts["verified" if ok else "failed"] += 1
    return counts


# ---------------------------------------------------------------- human approval

class PlacementError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def approve(conn, series_id, placement_id, user_id, note: str = "") -> None:
    p = conn.execute("select * from placements where id = %s and series_id = %s for update", (placement_id, series_id)).fetchone()
    if p is None:
        raise PlacementError(404, "Placement not found.")
    if p["status"] not in ("verified", "failed", "rejected"):
        raise PlacementError(409, "This placement has not been rendered and checked yet.")
    if p["status"] != "verified" and len((note or "").strip()) < 5:
        raise PlacementError(400, "The machine checks did not pass for this placement. Write why you approve it anyway.")
    conn.execute("update placements set status = 'approved', approved_by = %s, approved_at = now(), review_note = %s where id = %s", (user_id, (note or "").strip() or None, placement_id))
    audit.log(conn, user_id, "placement.approve", series_id, {"placement": str(placement_id), "override": p["status"] != "verified"})


def reject(conn, series_id, placement_id, user_id, note: str) -> None:
    """A rejection becomes a confirmed `brand` feedback item, so the existing rework (send feedback) redraws the shot."""
    note = (note or "").strip()
    if not note:
        raise PlacementError(400, "Write what is wrong with the placement.")
    p = conn.execute("select * from placements where id = %s and series_id = %s for update", (placement_id, series_id)).fetchone()
    if p is None or not p["shot_id"]:
        raise PlacementError(404, "Placement not found.")
    if p["status"] == "rejected":
        raise PlacementError(409, "This placement was already rejected.")
    shot = conn.execute("select e.start_seconds, e.end_seconds from edit_entries e join episodes ep on ep.id = e.episode_id where e.shot_id = %s and e.cut_version = ep.cut_version", (p["shot_id"],)).fetchone()
    conn.execute("update placements set status = 'rejected', approved_by = null, approved_at = null, review_note = %s where id = %s", (note, placement_id))
    conn.execute("insert into feedback_items (series_id, episode_number, start_seconds, end_seconds, note, category, status, shot_ids, created_by) values (%s,%s,%s,%s,%s,'brand','confirmed',%s,%s)",
                 (series_id, p["episode_number"], int(shot["start_seconds"]) if shot else 0, int(shot["end_seconds"]) if shot else 0, f"Brand placement rejected: {note}", [p["shot_id"]], user_id))
    audit.log(conn, user_id, "placement.reject", series_id, {"placement": str(placement_id)})


def unapproved(series_id) -> list[dict]:
    """Visual placements that a person has not approved yet (Gate 5 cannot be approved while any remain)."""
    return db.fetch_all("select episode_number, kind, status from placements where series_id = %s and kind = any(%s) and status <> 'approved' order by episode_number", (series_id, list(VISUAL)))


# ---------------------------------------------------------------- the report

def report(series_id) -> list[dict]:
    """Every placement with its episode, shot, timestamps in the latest cut, seconds visible, fidelity and approval."""
    out = []
    for p in db.fetch_all("select p.*, u.display_name approver from placements p left join users u on u.id = p.approved_by where p.series_id = %s order by p.episode_number, p.created_at", (series_id,)):
        ep = db.fetch_one("select id, cut_version from episodes where series_id = %s and number = %s", (series_id, p["episode_number"]))
        start = end = idx = None
        clip = None
        if p["shot_id"] and ep:
            row = db.fetch_one("select e.start_seconds, e.end_seconds, s.idx, s.final_clip_r2_key from edit_entries e join shots s on s.id = e.shot_id where e.shot_id = %s and e.cut_version = %s", (p["shot_id"], ep["cut_version"]))
            if row:
                start, end, idx, clip = float(row["start_seconds"]), float(row["end_seconds"]), row["idx"], row["final_clip_r2_key"]
        fid = p["fidelity"] or {}
        out.append({"id": p["id"], "episode": p["episode_number"], "kind": p["kind"], "method": KIND_LABEL[p["kind"]], "description": p["description"], "status": p["status"],
                    "shot_id": p["shot_id"], "shot_idx": idx, "start": start, "end": end, "time": (f"{mmss(start)}-{mmss(end)}" if start is not None else ""),
                    "exposure": float(p["exposure_seconds"]) if p["exposure_seconds"] is not None else None, "label_read": fid.get("label_text_read"), "fidelity": fid,
                    "approved_by": p["approver"], "review_note": p["review_note"], "clip": clip})
    return out


def report_csv(series_id) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["episode", "placement", "status", "shot", "start", "end", "time", "seconds_visible", "label_read", "approved_by", "note", "description"])
    for r in report(series_id):
        w.writerow([r["episode"], r["kind"], r["status"], r["shot_idx"] if r["shot_idx"] is not None else "", f"{r['start']:.2f}" if r["start"] is not None else "",
                    f"{r['end']:.2f}" if r["end"] is not None else "", r["time"], f"{r['exposure']:.1f}" if r["exposure"] is not None else "", r["label_read"] or "",
                    r["approved_by"] or "", r["review_note"] or "", r["description"]])
    return buf.getvalue()
