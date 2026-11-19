"""Apply numbered migrations/*.sql in order, track them in schema_migrations, then set up the
LangGraph checkpointer tables. Safe to run any number of times."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402

MIGRATIONS_DIR = ROOT / "migrations"
LOCK_KEY = 7_364_001  # advisory lock so two processes never migrate at once


def apply_migrations(url: str | None = None, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Returns the names of migrations applied by this call."""
    applied_now: list[str] = []
    with db.connect(url, autocommit=True) as conn:
        conn.execute("select pg_advisory_lock(%s)", (LOCK_KEY,))
        try:
            conn.execute(
                "create table if not exists schema_migrations ("
                " name text primary key, applied_at timestamptz not null default now())"
            )
            done = {r["name"] for r in conn.execute("select name from schema_migrations").fetchall()}
            for path in sorted(migrations_dir.glob("*.sql")):
                if path.name in done:
                    continue
                with conn.transaction():
                    conn.execute(path.read_text())
                    conn.execute("insert into schema_migrations (name) values (%s)", (path.name,))
                applied_now.append(path.name)
            _setup_checkpointer(conn)
        finally:
            conn.execute("select pg_advisory_unlock(%s)", (LOCK_KEY,))
    return applied_now


def _setup_checkpointer(conn) -> None:
    from langgraph.checkpoint.postgres import PostgresSaver

    # The connection is autocommit with dict rows, as the saver requires.
    PostgresSaver(conn).setup()


if __name__ == "__main__":
    names = apply_migrations()
    print("applied:", ", ".join(names) if names else "nothing (already up to date)")
