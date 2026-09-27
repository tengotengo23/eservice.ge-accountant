import os
import uuid
from pathlib import Path

import pytest

SCHEMA = Path(__file__).resolve().parents[1] / "db" / "schema.sql"


@pytest.fixture(autouse=True)
def _dev_settings(monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(scope="session")
def pg_dsn():
    """TEST_DATABASE_URL - superuser DSN (მაგ. postgresql://postgres@localhost:5433/postgres).
    თითო სესიაზე იქმნება ცალკე ბაზა, მასზე ეშვება db/schema.sql და ბოლოს იშლება."""
    admin_dsn = os.environ.get("TEST_DATABASE_URL")
    if not admin_dsn:
        pytest.skip("TEST_DATABASE_URL not set")
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    name = f"ai_accountant_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    params = conninfo_to_dict(admin_dsn)
    params["dbname"] = name
    dsn = make_conninfo(**params)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(SCHEMA.read_text())
    yield dsn
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
