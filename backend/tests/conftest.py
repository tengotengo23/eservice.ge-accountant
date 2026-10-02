import os
import uuid

import pytest


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
    თითო სესიაზე იქმნება ცალკე ბაზა, მასზე ეშვება ყველა მიგრაცია და ბოლოს იშლება."""
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
    from app.db.migrate import migrate

    migrate(dsn)
    yield dsn
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
