"""Insert default config rows. Existing rows are left alone unless --force is given."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from psycopg.types.json import Jsonb  # noqa: E402

from app import db  # noqa: E402
from app.config_defaults import CONFIG_DEFAULTS  # noqa: E402


def seed(url: str | None = None, force: bool = False) -> int:
    """Returns the number of rows written."""
    written = 0
    with db.connect(url) as conn:
        for key, (value, note) in CONFIG_DEFAULTS.items():
            if force:
                sql = ("insert into config (key, value, note) values (%s, %s, %s) "
                       "on conflict (key) do update set value = excluded.value, note = excluded.note")
            else:
                sql = "insert into config (key, value, note) values (%s, %s, %s) on conflict (key) do nothing"
            written += conn.execute(sql, (key, Jsonb(value), note)).rowcount
        conn.commit()
    return written


if __name__ == "__main__":
    print(f"seeded {seed(force='--force' in sys.argv)} config rows")
