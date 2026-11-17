"""Connection pool and plain-SQL helpers. No ORM.

All connections use prepare_threshold=None and dict rows. That keeps psycopg working
through a Supabase pooler (prepared statements break in transaction mode), and the
guide's DATABASE_URL must be a direct or session-pooler (port 5432) address.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import get_settings

_pool: ConnectionPool | None = None


def _url(url: str | None) -> str:
    url = url or get_settings().database_url
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url


def connect(url: str | None = None, autocommit: bool = False) -> psycopg.Connection:
    """One standalone connection (scripts, tests)."""
    return psycopg.connect(_url(url), autocommit=autocommit, row_factory=dict_row, prepare_threshold=None)


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            _url(None),
            min_size=1,
            max_size=10,
            kwargs={"row_factory": dict_row, "prepare_threshold": None},
            open=True,
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def transaction() -> Iterator[psycopg.Connection]:
    """A pooled connection inside one transaction (commit on success, rollback on error)."""
    with get_pool().connection() as conn:
        yield conn


def fetch_one(sql: str, params: Any = None) -> dict | None:
    with get_pool().connection() as conn:
        return conn.execute(sql, params).fetchone()


def fetch_all(sql: str, params: Any = None) -> list[dict]:
    with get_pool().connection() as conn:
        return conn.execute(sql, params).fetchall()


def execute(sql: str, params: Any = None) -> int:
    with get_pool().connection() as conn:
        return conn.execute(sql, params).rowcount
