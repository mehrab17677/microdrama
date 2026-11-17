"""Environment variables and the config-table loader."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    database_url: str | None
    dry_run: bool
    public_base_url: str | None
    session_secret: str | None
    worker_concurrency: int
    media_tmp_dir: str
    r2_account_id: str | None
    r2_access_key_id: str | None
    r2_secret_access_key: str | None
    r2_bucket: str | None

    @property
    def r2_configured(self) -> bool:
        return all([self.r2_account_id, self.r2_access_key_id, self.r2_secret_access_key, self.r2_bucket])


def get_settings() -> Settings:
    """Read the environment on every call so tests can change it."""
    return Settings(
        database_url=_env("DATABASE_URL"),
        dry_run=_env("DRY_RUN", "0") == "1",
        public_base_url=_env("PUBLIC_BASE_URL"),
        session_secret=_env("SESSION_SECRET"),
        worker_concurrency=int(_env("WORKER_CONCURRENCY", "4")),
        media_tmp_dir=_env("MEDIA_TMP_DIR", "/tmp/microdrama"),
        r2_account_id=_env("R2_ACCOUNT_ID"),
        r2_access_key_id=_env("R2_ACCESS_KEY_ID"),
        r2_secret_access_key=_env("R2_SECRET_ACCESS_KEY"),
        r2_bucket=_env("R2_BUCKET"),
    )


def dry_run() -> bool:
    return get_settings().dry_run


_CACHE: dict[str, tuple[float, object]] = {}
_TTL_SECONDS = 30.0


def get_config(key: str, conn=None):
    """Return the jsonb value of a config row (cached for 30 s). Raises KeyError if missing."""
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < _TTL_SECONDS:
        return hit[1]
    from app import db

    row = (conn.execute("select value from config where key = %s", (key,)).fetchone() if conn else db.fetch_one("select value from config where key = %s", (key,)))
    if row is None:
        raise KeyError(f"config key {key!r} is not seeded; run scripts/seed_config.py")
    _CACHE[key] = (now, row["value"])
    return row["value"]


def clear_config_cache() -> None:
    _CACHE.clear()
