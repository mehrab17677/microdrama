"""All tests run with DRY_RUN=1 against a throw-away Postgres database (pgvector required)."""
import os

import pytest

os.environ["DRY_RUN"] = "1"
TEST_URL = os.environ.get("TEST_DATABASE_URL", "postgresql://postgres@localhost:5433/microdrama_test")
os.environ["DATABASE_URL"] = TEST_URL


@pytest.fixture(scope="session")
def db_url():
    from app import db

    with db.connect(TEST_URL, autocommit=True) as conn:
        conn.execute("drop schema public cascade")
        conn.execute("create schema public")
    return TEST_URL


@pytest.fixture(autouse=True)
def _no_real_r2(monkeypatch):
    """Tests never touch the real bucket, even when R2 variables are set in the shell."""
    for name in ("R2_ACCOUNT_ID", "R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("IMAGE_BACKEND", raising=False)      # tests use fixtures, never real generation
    monkeypatch.delenv("EMBEDDING_BACKEND", raising=False)
    monkeypatch.delenv("VOICE_BACKEND", raising=False)
    monkeypatch.delenv("SKIP_PACK_KINDS", raising=False)
