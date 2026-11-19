"""Ad placement (pipeline doc "Ad placement track", guide phase 11).

Brand assets are reproduced exactly and never restyled. The first version builds two FULL-FRAME inserts with FFmpeg, no video model:
  phone_full    each app_screen asset in order, scaled to fit 1080x1920 on the brand colour, hard cuts
  product_full  each product asset the same way
Each asset is held for planned / number_of_assets seconds. Composited placements (phone in hand, signage) and spoken mentions are
planned and reported, but the composite is an open item: they are listed for human review and never silently passed.
This module holds the pure pieces: composing a frame, building and verifying an insert, the report and the placement status.
"""
from __future__ import annotations

import io
import statistics
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

from app import db
from app.media import ffmpeg

W, H, FPS = 1080, 1920, 24
FULL_FRAME = {"phone_full": "app_screen", "product_full": "product"}
MANUAL = ("phone_in_hand", "signage")           # composited placements: not built in the first version
DEFAULT_COLOR = "#FFFFFF"
PIXEL_DIFF_MAX = 6.0                              # mean absolute difference (0 to 255) allowed between a verified frame and the expected frame
MIN_HOLD = 0.8                                    # seconds each asset needs to be readable


def hex_to_rgb(color: str | None) -> tuple[int, int, int]:
    c = (color or DEFAULT_COLOR).lstrip("#")
    if len(c) != 6:
        c = DEFAULT_COLOR.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def compose_frame(asset: bytes, color: str | None) -> Image.Image:
    """The asset scaled to fit 1080x1920 (never cropped, never stretched) on the brand colour. Transparency is flattened onto that colour."""
    src = Image.open(io.BytesIO(asset))
    src.load()
    src = src.convert("RGBA")
    scale = min(W / src.width, H / src.height)
    size = (max(1, round(src.width * scale)), max(1, round(src.height * scale)))
    resized = src.resize(size, Image.LANCZOS)
    canvas = Image.new("RGBA", (W, H), (*hex_to_rgb(color), 255))
    canvas.alpha_composite(resized, ((W - size[0]) // 2, (H - size[1]) // 2))
    return canvas.convert("RGB")


def holds(planned: float, n: int) -> list[float]:
    """n equal holds that add up to `planned` exactly (the last one takes the rounding)."""
    each = round(planned / n, 3)
    out = [each] * n
    out[-1] = round(planned - each * (n - 1), 3)
    return out


def build_insert(assets: list[bytes], color: str | None, planned: float, out: Path, workdir: Path) -> list[Path]:
    """Hard cuts between the composed screens; near-lossless (crf 10) so the assets stay as supplied. Returns the frame PNGs used."""
    from app.media.compile import preset

    pngs, parts = [], []
    for i, (data, hold) in enumerate(zip(assets, holds(planned, len(assets)))):
        frame = workdir / f"screen{i}.png"
        compose_frame(data, color).save(frame)
        part = workdir / f"part{i}.mp4"
        ffmpeg.run(["-loop", "1", "-framerate", str(FPS), "-t", f"{hold:.3f}", "-i", str(frame), "-vf", "format=yuv420p", "-an", "-c:v", "libx264", "-preset", preset(), "-crf", "10", str(part)])
        pngs.append(frame)
        parts.append(part)
    from app.media import compile as media_compile

    media_compile.concat(parts, out, workdir)
    return pngs


def frame_at(video: Path, t: float, out: Path) -> Image.Image:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1", str(out)], check=True)
    return Image.open(out).convert("RGB")


def mean_abs_diff(a: Image.Image, b: Image.Image) -> float:
    if a.size != b.size:
        return 255.0
    return float(np.abs(np.asarray(a, dtype=np.int16) - np.asarray(b, dtype=np.int16)).mean())


def verify_insert(clip: Path, assets: list[bytes], color: str | None, planned: float, limit: float = PIXEL_DIFF_MAX) -> tuple[bool, float, dict]:
    """Frame-grab the middle of every hold and compare it with the asset composed the same way. Passes when every screen is within `limit`."""
    diffs = []
    with tempfile.TemporaryDirectory() as tmp:
        t = 0.0
        for i, (data, hold) in enumerate(zip(assets, holds(planned, len(assets)))):
            got = frame_at(clip, t + hold / 2, Path(tmp) / f"g{i}.png")
            diffs.append(round(mean_abs_diff(got, compose_frame(data, color)), 3))
            t += hold
    worst = max(diffs) if diffs else 255.0
    return worst <= limit, worst, {"screens": len(assets), "diffs": diffs, "limit": limit}


def asset_rows(series_id, kind: str) -> list[dict]:
    return db.fetch_all("select * from brand_assets where series_id = %s and kind = %s order by created_at, id", (series_id, kind))


def brand_color(series_id) -> str:
    row = db.fetch_one("select brand_color from brand_briefs where series_id = %s order by version desc limit 1", (series_id,))
    return (row or {}).get("brand_color") or DEFAULT_COLOR


def mmss(t: float) -> str:
    t = int(round(t))
    return f"{t // 60:02d}:{t % 60:02d}"


# ---------------------------------------------------------------- placement status and the report

def link_shots(series_id, episode_number: int) -> None:
    """After a shot list is stored: tie each planned placement to the first shot of its kind."""
    ep = db.fetch_one("select id from episodes where series_id = %s and number = %s", (series_id, episode_number))
    for p in db.fetch_all("select id, kind from placements where series_id = %s and episode_number = %s", (series_id, episode_number)):
        shot = db.fetch_one("select id from shots where episode_id = %s and spec->>'placement_kind' = %s order by idx limit 1", (ep["id"], p["kind"]))
        db.execute("update placements set shot_id = %s, status = 'planned' where id = %s", (shot and shot["id"], p["id"]))


def mark_placed(series_id, shot_id) -> None:
    db.execute("update placements set status = 'placed' where series_id = %s and shot_id = %s and status in ('planned', 'failed')", (series_id, shot_id))


def report(series_id) -> list[dict]:
    """Every placement with its episode, shot and timestamps in the episode's latest cut (guide: Gates 4 and 5, delivery)."""
    out = []
    for p in db.fetch_all("select * from placements where series_id = %s order by episode_number, created_at", (series_id,)):
        ep = db.fetch_one("select id, cut_version from episodes where series_id = %s and number = %s", (series_id, p["episode_number"]))
        start = end = None
        idx = None
        if p["shot_id"] and ep:
            row = db.fetch_one("select e.start_seconds, e.end_seconds, s.idx from edit_entries e join shots s on s.id = e.shot_id where e.shot_id = %s and e.cut_version = %s", (p["shot_id"], ep["cut_version"]))
            if row:
                start, end, idx = float(row["start_seconds"]), float(row["end_seconds"]), row["idx"]
        assets = []
        if p["kind"] in FULL_FRAME:
            assets = [a["label"] for a in asset_rows(series_id, FULL_FRAME[p["kind"]])]
        method = {"phone_full": "Full-frame phone insert from the supplied app screens", "product_full": "Full-frame insert from the supplied product images",
                  "spoken": "Spoken mention in the dialogue", "phone_in_hand": "Composited phone in hand (not built yet: human review)",
                  "signage": "Composited logo on a sign (not built yet: human review)"}[p["kind"]]
        out.append({"episode": p["episode_number"], "kind": p["kind"], "method": method, "description": p["description"], "status": p["status"], "shot_idx": idx,
                    "start": start, "end": end, "time": (f"{mmss(start)}-{mmss(end)}" if start is not None else ""), "assets": assets})
    return out


def report_csv(series_id) -> str:
    import csv

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["episode", "placement", "method", "status", "shot", "start", "end", "time", "assets", "description"])
    for r in report(series_id):
        w.writerow([r["episode"], r["kind"], r["method"], r["status"], r["shot_idx"] if r["shot_idx"] is not None else "", f"{r['start']:.2f}" if r["start"] is not None else "",
                    f"{r['end']:.2f}" if r["end"] is not None else "", r["time"], "; ".join(r["assets"]), r["description"]])
    return buf.getvalue()


def finalize(series_id, batch: str) -> dict:
    """After QA: a placement is 'verified' when its episode's brand checks and its shot's asset check passed, otherwise 'failed'.
    Manual kinds stay 'placed' (never verified by a machine)."""
    counts = {"verified": 0, "failed": 0}
    for p in db.fetch_all("select p.* from placements p join episodes e on e.series_id = p.series_id and e.number = p.episode_number where p.series_id = %s and e.batch = %s", (series_id, batch)):
        if p["kind"] in MANUAL:
            continue
        ep = db.fetch_one("select id from episodes where series_id = %s and number = %s", (series_id, p["episode_number"]))
        episode_checks = db.fetch_all("select check_name, passed from qa_results where episode_id = %s and shot_id is null and check_name in ('placement_count','exclusions')"
                                      " and qa_round in ('qa1','qa2') order by created_at desc", (ep["id"],))
        latest = {}
        for c in episode_checks:
            latest.setdefault(c["check_name"], c["passed"])
        shot_ok = True
        if p["shot_id"] and p["kind"] in FULL_FRAME:
            rows = db.fetch_all("select passed, detail from qa_results where shot_id = %s and check_name = 'asset_intact' and qa_round in ('qa1','qa2')", (p["shot_id"],))
            if rows:
                newest = max(rows, key=lambda r: (int(r["detail"].get("attempts", 0)), int(r["detail"].get("sync_attempts", 0))))
                shot_ok = newest["passed"]
            else:
                shot_ok = False
        ok = shot_ok and bool(latest) and all(latest.values())
        db.execute("update placements set status = %s where id = %s", ("verified" if ok else "failed", p["id"]))
        counts["verified" if ok else "failed"] += 1
    return counts
