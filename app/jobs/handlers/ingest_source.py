"""ingest_source: one library source (guide 9.3). Vendors: anthropic, openai."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from app import db, library
from app.jobs.errors import PermanentError
from app.stages import ingest
from app.vendors import storage

KINDS = ["ingest_source"]


def _load(job: dict) -> dict:
    row = db.fetch_one("select * from sources where id = %s", (job["request"]["source_id"],))
    if row is None:
        raise PermanentError("the source no longer exists")
    return row


def run(job: dict) -> dict:
    source = _load(job)
    db.execute("update sources set status = 'processing', error = null where id = %s", (source["id"],))
    tmp = Path(tempfile.mkdtemp(prefix="ingest_"))
    try:
        local = tmp / "source"
        storage.get_to_file(source["r2_key"], local)
        result = ingest.ingest(source, local)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    with db.transaction() as conn:
        library.check_batch_done(conn, source["batch_id"])
    return result


def on_failure(job: dict, error: str) -> None:
    """Called by the worker after the job failed for good: mark the source and maybe finish the batch."""
    source_id = job["request"].get("source_id")
    with db.transaction() as conn:
        row = conn.execute(
            "update sources set status = 'failed', error = %s where id = %s returning batch_id", (error[:500], source_id)
        ).fetchone()
        if row:
            library.check_batch_done(conn, row["batch_id"])
