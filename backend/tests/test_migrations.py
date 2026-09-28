"""მიგრაციები + სქემის "lint": ყოველ ცხრილს აქვს RLS და აპლიკაციის როლის უფლებები (მომავალი მიგრაციების დაცვა)."""

import os
import uuid

import psycopg
import pytest

from app.db.migrate import Migration, MigrationError, discover, migrate

pytestmark = pytest.mark.postgres


@pytest.fixture
def fresh_db():
    admin = os.environ.get("TEST_DATABASE_URL")
    if not admin:
        pytest.skip("TEST_DATABASE_URL not set")
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    name = f"mig_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    yield make_conninfo(**{**conninfo_to_dict(admin), "dbname": name})
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def test_migrations_are_numbered_and_apply_once(fresh_db):
    names = [m.name for m in discover()]
    assert names[:2] == ["0001_initial.sql", "0002_worker.sql"]
    assert migrate(fresh_db) == names
    assert migrate(fresh_db) == []  # idempotent - ხელახლა არაფერი ეშვება
    with psycopg.connect(fresh_db) as c:
        assert [r[0] for r in c.execute("SELECT version FROM meta.schema_migrations ORDER BY 1")] == \
            [m.version for m in discover()]


def test_editing_an_applied_migration_is_refused(fresh_db):
    migrations = discover()
    migrate(fresh_db, migrations[:1])
    tampered = Migration(1, migrations[0].name, migrations[0].sql + "\n-- edited")
    with pytest.raises(MigrationError, match="modified after it was applied"):
        migrate(fresh_db, [tampered])


def test_failed_migration_rolls_back_completely(fresh_db):
    migrate(fresh_db)
    broken = Migration(9001, "9001_broken.sql", "CREATE TABLE half_done (id int); SELECT 1/0;")
    with pytest.raises(psycopg.errors.DivisionByZero):
        migrate(fresh_db, [*discover(), broken])
    with psycopg.connect(fresh_db) as c:
        assert c.execute("SELECT to_regclass('public.half_done')").fetchone()[0] is None
        assert c.execute("SELECT count(*) FROM meta.schema_migrations WHERE version = 9001").fetchone()[0] == 0


def test_every_table_is_tenant_isolated_and_granted(pg_dsn):
    """ახალ მიგრაციაში RLS-ის ან GRANT-ის დავიწყება აქ ჩავარდება, არა production-ში."""
    with psycopg.connect(pg_dsn) as c:
        tables = c.execute(
            "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,"
            " has_table_privilege('ai_accountant_app', c.oid, 'SELECT') AS can_read,"
            " EXISTS (SELECT 1 FROM pg_policy p WHERE p.polrelid = c.oid) AS has_policy"
            " FROM pg_class c WHERE c.relkind = 'r' AND c.relnamespace = 'public'::regnamespace").fetchall()
        meta_visible = c.execute("SELECT has_table_privilege('ai_accountant_app', 'meta.schema_migrations', 'SELECT')"
                                 ).fetchone()[0]
    problems = [name for name, rls, force, can_read, has_policy in tables
                if not (rls and force and can_read and has_policy)]
    assert len(tables) >= 20 and problems == []
    assert meta_visible is False


def test_scheduler_can_list_tenant_ids_but_nothing_else(pg_dsn):
    ids = [uuid.uuid4(), uuid.uuid4()]
    with psycopg.connect(pg_dsn, autocommit=True) as c:
        for i in ids:
            c.execute("INSERT INTO tenants (id, name) VALUES (%s, 'x')", (i,))
    with psycopg.connect(pg_dsn) as c:
        c.execute("SET ROLE ai_accountant_app")
        assert c.execute("SELECT count(*) FROM tenants").fetchone()[0] == 0  # RLS: tenant-ის კონტექსტის გარეშე
        visible = {r[0] for r in c.execute("SELECT system_tenant_ids()")}
    assert set(ids) <= visible


def test_migrations_run_as_non_superuser_owner_like_managed_postgres():
    """RDS/Cloud SQL: მიგრაციის როლი superuser არ არის, FORCE RLS მასზეც მოქმედებს."""
    admin = os.environ.get("TEST_DATABASE_URL")
    if not admin:
        pytest.skip("TEST_DATABASE_URL not set")
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    suffix = uuid.uuid4().hex[:8]
    role, name = f"mig_owner_{suffix}", f"mig_nosu_{suffix}"
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(f'CREATE ROLE "{role}" LOGIN PASSWORD \'x\' NOSUPERUSER NOBYPASSRLS')
        c.execute(f'CREATE DATABASE "{name}" OWNER "{role}"')
        c.execute("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ai_accountant_app') THEN"
                  " CREATE ROLE ai_accountant_app NOLOGIN; END IF; END $$")
        c.execute(f'GRANT ai_accountant_app TO "{role}"')  # მხოლოდ შემოწმებისთვის (SET ROLE)
    params = conninfo_to_dict(admin)
    owner_dsn = make_conninfo(**{**params, "dbname": name, "user": role, "password": "x"})
    try:
        assert len(migrate(owner_dsn)) == len(discover())
        tenant = uuid.uuid4()
        with psycopg.connect(owner_dsn) as c:
            c.execute("SELECT set_config('app.tenant_id', %s, false)", (str(tenant),))
            c.execute("INSERT INTO tenants (id, name) VALUES (%s, 'x')", (tenant,))
            c.commit()
            c.execute("SET ROLE ai_accountant_app")
            c.execute("SELECT set_config('app.tenant_id', '', false)")
            assert [r[0] for r in c.execute("SELECT system_tenant_ids()")] == [tenant]
    finally:
        with psycopg.connect(admin, autocommit=True) as c:
            c.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
            c.execute(f'DROP OWNED BY "{role}"')
            c.execute(f'DROP ROLE "{role}"')
