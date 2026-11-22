import re
from pathlib import Path

import pytest

from app import db
from app.config_defaults import CONFIG_DEFAULTS
from app.models import enums
from scripts import migrate, preflight, seed_config

ROOT = Path(__file__).resolve().parent.parent


def snapshot(url):
    with db.connect(url) as conn:
        tables = sorted(r["tablename"] for r in conn.execute("select tablename from pg_tables where schemaname='public'").fetchall())
        cols = conn.execute("select count(*) n from information_schema.columns where table_schema='public'").fetchone()["n"]
        migs = sorted(r["name"] for r in conn.execute("select name from schema_migrations").fetchall())
    return tables, cols, migs


def test_trivial():
    assert True


def test_migrations_twice_change_nothing(db_url):
    with db.connect(db_url, autocommit=True) as conn:  # start from nothing, whatever ran before
        conn.execute("drop schema public cascade")
        conn.execute("create schema public")
    first = migrate.apply_migrations(db_url)
    assert "0001_init.sql" in first
    before = snapshot(db_url)
    second = migrate.apply_migrations(db_url)
    assert second == []
    assert snapshot(db_url) == before
    assert {"users", "jobs", "series", "shots", "checkpoints"} <= set(before[0])


def test_seed_config_is_idempotent_and_complete(db_url):
    migrate.apply_migrations(db_url)
    seed_config.seed(db_url)
    with db.connect(db_url) as conn:
        conn.execute("update config set value = '99'::jsonb where key = 'max_attempts'")
        conn.commit()
    assert seed_config.seed(db_url) == 0  # existing (tuned) rows are not overwritten
    with db.connect(db_url) as conn:
        rows = {r["key"]: r["value"] for r in conn.execute("select key, value from config").fetchall()}
    assert set(rows) == set(CONFIG_DEFAULTS)
    assert rows["max_attempts"] == 99
    assert seed_config.seed(db_url, force=True) == len(CONFIG_DEFAULTS)


CHECKS = [
    ("series", "status", enums.SeriesStatus),
    ("series", "current_stage", enums.SeriesStage),
    ("episodes", "status", enums.EpisodeStatus),
    ("shots", "status", enums.ShotStatus),
    ("shots", "tier", enums.Tier),
    ("shots", "shot_type", enums.ShotType),
    ("jobs", "status", enums.JobStatus),
    ("jobs", "phase", enums.JobPhase),
    ("jobs", "budget_class", enums.BudgetClass),
    ("users", "role", enums.UserRole),
    ("users", "status", enums.UserStatus),
    ("series", "category", enums.Category),
    ("series", "language", enums.Language),
    ("feedback_items", "category", enums.FeedbackCategory),
    ("feedback_items", "status", enums.FeedbackStatus),
    ("placements", "kind", enums.PlacementKind),
    ("qa_results", "qa_round", enums.QaRound),
]


@pytest.mark.parametrize("table,column,enum", CHECKS)
def test_enums_match_database_checks(db_url, table, column, enum):
    migrate.apply_migrations(db_url)
    with db.connect(db_url) as conn:
        defs = [r["d"] for r in conn.execute(
            "select pg_get_constraintdef(oid) d from pg_constraint where conrelid = %s::regclass and contype = 'c'", (table,)
        ).fetchall()]
    found = next((set(re.findall(r"'([^']+)'", d)) for d in defs if re.search(rf"\b{column}\b", d)), None)
    assert found == {e.value for e in enum}


def test_enum_order_matches_guide():
    assert [e.value for e in enums.SeriesStatus] == ["running", "waiting_jobs", "waiting_gate", "held_budget", "done", "discarded", "failed"]
    assert [e.value for e in enums.SeriesStage][0:3] == ["gate0", "gate1", "story"]


def test_second_admin_is_rejected(db_url):
    migrate.apply_migrations(db_url)
    import psycopg

    with db.connect(db_url) as conn:
        conn.execute("insert into users (email, display_name, password_hash, role, status) values ('a@x.io','A','h','admin','active')")
        conn.commit()
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute("insert into users (email, display_name, password_hash, role, status) values ('b@x.io','B','h','admin','active')")


def test_preflight_passes_in_dry_run(db_url):
    migrate.apply_migrations(db_url)
    seed_config.seed(db_url)
    results = preflight.run_all(dry_run=True)
    failed = [(n, m) for n, s, m in results if s == preflight.FAIL]
    assert not failed, failed
    assert {n for n, s, _ in results if s == preflight.SKIP} == {"r2", "anthropic", "openai", "embeddings", "elevenlabs", "fal"}


def test_web_health():
    from fastapi.testclient import TestClient

    from app.web.main import app

    assert TestClient(app).get("/healthz").json() == {"ok": True}
