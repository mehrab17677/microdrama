"""Retention (Phase 12): delete raw and failed clips older than config key raw_retention_days (guide 12 and the pipeline doc: "Raw clips and failed takes are deleted
after a retention period; finished episodes are kept").

Deleted: raw_aN.mp4 and synced_aN.mp4 under a shot's folder, older than the limit, in a production that has ended (done, discarded or failed).
Never deleted: any clip a shot uses as its final clip, the final episode files, subtitles, audio, keyframes, brand assets, master, reports, and anything in a production still running
(its raw clips may still be needed for a lip-sync redo)."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from app import db
from app.config import get_config
from app.vendors import storage

CLIP = re.compile(r"^series/[^/]+/episodes/\d+/shots/\d+/(raw|synced)_a\d+\.mp4$")
ENDED = ("done", "discarded", "failed")


def run(dry_run: bool = False, now: datetime | None = None) -> dict:
    days = int(get_config("raw_retention_days"))
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    deleted = kept_recent = kept_in_use = 0
    freed = 0
    for s in db.fetch_all("select id from series where status = any(%s)", (list(ENDED),)):
        in_use = {r["final_clip_r2_key"] for r in db.fetch_all("select final_clip_r2_key from shots where series_id = %s and final_clip_r2_key is not null", (s["id"],))}
        for key, modified, size in list(storage.list_objects(f"series/{s['id']}/episodes/")):
            if not CLIP.match(key):
                continue
            if key in in_use:
                kept_in_use += 1
            elif modified > cutoff:
                kept_recent += 1
            else:
                if not dry_run:
                    storage.delete(key)
                deleted += 1
                freed += size
    return {"retention_days": days, "deleted": deleted, "freed_bytes": freed, "kept_recent": kept_recent, "kept_in_use": kept_in_use, "dry_run": dry_run}
