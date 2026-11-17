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
