"""Lore library: genres, bulk upload, batch reports (guide 9.3, 12.3, 12.7)."""
from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import PurePosixPath

from app import alerts, db
from app.config import get_config
from app.jobs import ledger
from app.models.enums import AlertKind, JobKind, Vendor
from app.stages.ingest import SUPPORTED_SUFFIXES
from app.text import normalize_genre
from app.vendors import storage

_LOCK_NAMESPACE = 7_364_004


def clean_file_name(name: str) -> str:
    base = PurePosixPath((name or "").replace("\\", "/")).name
    base = re.sub(r"[^\w.\- ()ঀ-৿]", "_", base).strip(" .")
    return base or "unnamed"


def get_or_create_genre(conn, name: str, user_id) -> dict:
    """One normalisation function for insert and lookup, so 'Revenge romance' and 'revenge-romance'
    are the same genre. The first spelling is kept as the display name."""
    norm = normalize_genre(name)
    if not norm:
        raise ValueError("Enter a genre.")
    conn.execute(
        "insert into genres (name, name_normalized, created_by) values (%s, %s, %s) on conflict (name_normalized) do nothing",
        (re.sub(r"\s+", " ", name.strip()), norm, user_id),
    )
    return conn.execute("select * from genres where name_normalized = %s", (norm,)).fetchone()


def upload_batch(user_id, genre_name: str, files: list[tuple[str, bytes]]) -> uuid.UUID:
    """Create one source per file and enqueue ingestion. Duplicates (by SHA-256) are recorded as
    skipped and reported; unsupported or oversized files are recorded as failed. Returns the batch id."""
    if not files:
        raise ValueError("Choose at least one file.")
    batch_id = uuid.uuid4()
    max_bytes = int(get_config("max_upload_mb")) * 1024 * 1024
    with db.transaction() as conn:
        genre = get_or_create_genre(conn, genre_name, user_id)
        queued = 0
        for raw_name, data in files:
            name = clean_file_name(raw_name)
            sha = hashlib.sha256(data).hexdigest()
            source_id = uuid.uuid4()
            key = f"sources/{source_id}/{name}"
            base = dict(id=source_id, genre_id=genre["id"], uploaded_by=user_id, batch_id=batch_id, file_name=name, r2_key=key)

            def insert(status, sha_value, error=None, r2_key=key):
                return conn.execute(
                    "insert into sources (id, genre_id, uploaded_by, batch_id, file_name, file_sha256, r2_key, status, error)"
                    " values (%(id)s, %(genre_id)s, %(uploaded_by)s, %(batch_id)s, %(file_name)s, %(sha)s, %(r2_key)s, %(status)s, %(error)s)"
                    " on conflict (file_sha256) do nothing returning id",
                    {**base, "sha": sha_value, "status": status, "error": error, "r2_key": r2_key},
                ).fetchone()

            if PurePosixPath(name).suffix.lower() not in SUPPORTED_SUFFIXES:
                insert("failed", f"{sha}:rejected:{source_id}", "Unsupported file type. Use .txt, .epub or .pdf.")
                continue
            if len(data) > max_bytes:
                insert("failed", f"{sha}:rejected:{source_id}", f"File is larger than {max_bytes // (1024 * 1024)} MB.")
                continue
            # A failed earlier attempt must not block a retry of the same file.
            conn.execute(
                "update sources set file_sha256 = file_sha256 || ':failed:' || id::text where file_sha256 = %s and status = 'failed'", (sha,)
            )
            if insert("queued", sha):
                storage.put_bytes(key, data, "application/octet-stream")
                ledger.enqueue(
                    JobKind.ingest_source, wait_group=str(batch_id), vendor=Vendor.anthropic, request={"source_id": str(source_id)}, conn=conn,
                )
                queued += 1
            else:
                original = conn.execute("select id, file_name, r2_key from sources where file_sha256 = %s", (sha,)).fetchone()
                insert("skipped_duplicate", f"{sha}:dup:{source_id}", f"Already in the library as '{original['file_name']}'.", original["r2_key"])
        alerts.notify([user_id], AlertKind.action_started, f"Library batch started: {len(files)} file(s).", f"/library?batch={batch_id}", conn=conn)
        check_batch_done(conn, batch_id)  # a batch with nothing to ingest finishes at once
    return batch_id


def check_batch_done(conn, batch_id) -> bool:
    """When the last source of a batch finishes, alert the uploader once with the counts."""
    conn.execute("select pg_advisory_xact_lock(%s, hashtext(%s))", (_LOCK_NAMESPACE, str(batch_id)))
    if conn.execute("select 1 from sources where batch_id = %s and status in ('queued','processing') limit 1", (batch_id,)).fetchone():
        return False
    link = f"/library?batch={batch_id}"
    if conn.execute("select 1 from alerts where kind = 'action_finished' and link = %s limit 1", (link,)).fetchone():
        return False
    rows = conn.execute("select status, count(*) n, min(uploaded_by::text) uploader from sources where batch_id = %s group by status", (batch_id,)).fetchall()
    if not rows:
        return False
    counts = {r["status"]: r["n"] for r in rows}
    uploader = conn.execute("select uploaded_by from sources where batch_id = %s limit 1", (batch_id,)).fetchone()["uploaded_by"]
    if uploader is None:
        return False
    ready, skipped, failed = counts.get("ready", 0), counts.get("skipped_duplicate", 0), counts.get("failed", 0)
    alerts.notify([uploader], AlertKind.action_finished, f"Library batch finished: {ready} ready, {skipped} skipped, {failed} failed.", link, conn=conn)
    return True


def genre_suggestions(query: str, ready_only: bool = False, limit: int = 10) -> list[dict]:
    """Genres whose normalised name matches what is being typed (prefix matches first)."""
    q = normalize_genre(query)
    like = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    having = "having count(*) filter (where s.status = 'ready') > 0" if ready_only else ""
    return db.fetch_all(
        f"select g.id, g.name, count(*) filter (where s.status = 'ready') ready_sources, count(s.id) total_sources"
        f" from genres g left join sources s on s.genre_id = g.id"
        f" where g.name_normalized like %s escape '\\' group by g.id {having}"
        f" order by (g.name_normalized like %s escape '\\') desc, g.name limit %s",
        (f"%{like}%", f"{like}%", limit),
    )


def ready_genres() -> list[dict]:
    """The genres a production may choose: only those with at least one ready source."""
    return genre_suggestions("", ready_only=True, limit=1000)


def recent_batches(limit: int = 10) -> list[dict]:
    batches = db.fetch_all(
        "select batch_id, min(created_at) started, min(uploaded_by::text) uploader from sources where series_id is null"
        " group by batch_id order by started desc limit %s", (limit,),
    )
    for b in batches:
        b["files"] = db.fetch_all(
            "select s.id, s.file_name, s.status, s.error, g.name genre from sources s left join genres g on g.id = s.genre_id"
            " where s.batch_id = %s order by s.created_at, s.file_name", (b["batch_id"],),
        )
    return batches
