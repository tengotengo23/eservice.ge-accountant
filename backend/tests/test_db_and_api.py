"""PostgreSQL სქემა (RLS, ბალანსი, დადასტურება, აუდიტი) + API-ის გასაღების ნაკადი. სჭირდება TEST_DATABASE_URL."""

import asyncio
import os
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_db, get_otp_broker, get_router, get_secret_box
from app.db.database import Database, audit
from app.llm import LLMAuthError, LLMRouter, Provider
from app.main import create_app
from app.rsge.otp import InMemoryOtpBroker
from app.security.crypto import LocalKeyProvider, SecretBox
from app.services.llm_keys import NoLlmCredentialError, load_default_credentials, register_llm_key

pytestmark = pytest.mark.postgres


def seed(dsn: str) -> dict:
    ids = {k: uuid.uuid4() for k in ("tenant_a", "tenant_b", "user", "tp_a", "tp_b")}
    with psycopg.connect(dsn, autocommit=True) as c:  # superuser - RLS-ს გვერდს უვლის (მხოლოდ seed)
        c.execute("INSERT INTO users (id, email) VALUES (%s, %s)", (ids["user"], f"{ids['user']}@example.ge"))
        for t, tp, tin in (("tenant_a", "tp_a", "405123456"), ("tenant_b", "tp_b", "205000111")):
            c.execute("INSERT INTO tenants (id, name) VALUES (%s, %s)", (ids[t], t))
            c.execute("INSERT INTO taxpayers (id, tenant_id, tin, legal_name, legal_form, regimes)"
                      " VALUES (%s, %s, %s, %s, 'llc', '{vat_payer}')", (ids[tp], ids[t], tin, f"შპს {t}"))
            c.execute("INSERT INTO chart_of_accounts (tenant_id, code, name_ka, type) VALUES"
                      " (%s, '1210', 'ბანკი', 'asset'), (%s, '6110', 'შემოსავალი', 'revenue')", (ids[t], ids[t]))
        c.execute("INSERT INTO tenant_members (tenant_id, user_id, role) VALUES (%s, %s, 'owner')",
                  (ids["tenant_a"], ids["user"]))
    return ids


@pytest.fixture
async def db(pg_dsn):
    database = Database(pg_dsn, set_role="ai_accountant_app")
    await database.open()
    yield database
    await database.close()


@pytest.fixture
def ids(pg_dsn):
    return seed(pg_dsn)


# ------------------------------------------------------------------ schema guarantees


async def test_rls_isolates_tenants(db, ids):
    async with db.tenant_tx(ids["tenant_a"]) as conn:
        rows = await (await conn.execute("SELECT tin FROM taxpayers")).fetchall()
        assert [r["tin"] for r in rows] == ["405123456"]
        assert len(await (await conn.execute("SELECT id FROM users")).fetchall()) == 1
        with pytest.raises(psycopg.errors.InsufficientPrivilege):  # WITH CHECK: სხვის tenant_id-ით ჩაწერა
            await conn.execute("INSERT INTO taxpayers (tenant_id, tin, legal_name, legal_form)"
                               " VALUES (%s, '111222333', 'x', 'llc')", (ids["tenant_b"],))


async def test_users_visible_only_to_their_tenants(db, ids):
    async with db.tenant_tx(ids["tenant_b"]) as conn:
        assert await (await conn.execute("SELECT id FROM users")).fetchall() == []


async def test_posted_entry_must_balance_and_is_immutable(db, ids):
    async def post(debit, credit):
        async with db.tenant_tx(ids["tenant_a"]) as conn:
            cur = await conn.execute(
                "INSERT INTO journal_entries (tenant_id, taxpayer_id, entry_date, description, status,"
                " created_by_type, posted_at) VALUES (%s, %s, '2026-09-01', 'გაყიდვა', 'posted', 'user', now())"
                " RETURNING id", (ids["tenant_a"], ids["tp_a"]))
            entry = (await cur.fetchone())["id"]
            await conn.execute("INSERT INTO journal_lines (tenant_id, entry_id, account_code, debit) VALUES"
                               " (%s, %s, '1210', %s)", (ids["tenant_a"], entry, debit))
            await conn.execute("INSERT INTO journal_lines (tenant_id, entry_id, account_code, credit) VALUES"
                               " (%s, %s, '6110', %s)", (ids["tenant_a"], entry, credit))
        return entry

    with pytest.raises(psycopg.errors.CheckViolation):
        await post("100.00", "99.99")
    entry = await post("118.00", "118.00")
    async with db.tenant_tx(ids["tenant_a"]) as conn:
        with pytest.raises(psycopg.errors.IntegrityConstraintViolation):
            await conn.execute("UPDATE journal_lines SET debit = 1 WHERE entry_id = %s AND debit > 0", (entry,))


async def test_declaration_cannot_be_submitted_without_matching_approval(db, ids):
    async with db.tenant_tx(ids["tenant_a"], actor_type="worker") as conn:
        cur = await conn.execute(
            "INSERT INTO declarations (tenant_id, taxpayer_id, type, period_start, period_end, due_date,"
            " status, is_zero, form_fingerprint) VALUES (%s, %s, 'vat', '2026-09-01', '2026-09-30', '2026-10-15',"
            " 'draft_prepared', true, 'abc') RETURNING id", (ids["tenant_a"], ids["tp_a"]))
        decl = (await cur.fetchone())["id"]
    async with db.tenant_tx(ids["tenant_a"], actor_type="worker") as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            await conn.execute("UPDATE declarations SET status = 'submitted', rs_receipt_number = 'X',"
                               " submitted_at = now() WHERE id = %s", (decl,))
    async with db.tenant_tx(ids["tenant_a"], actor_type="user", actor_id=ids["user"]) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):  # დადასტურებულია სხვა (ძველი) ფორმა
            await conn.execute("UPDATE declarations SET status = 'approved', approved_by = %s, approved_at = now(),"
                               " approved_fingerprint = 'OLD' WHERE id = %s", (ids["user"], decl))
    async with db.tenant_tx(ids["tenant_a"], actor_type="user", actor_id=ids["user"]) as conn:
        await conn.execute("UPDATE declarations SET status = 'approved', approved_by = %s, approved_at = now(),"
                           " approved_fingerprint = 'abc' WHERE id = %s", (ids["user"], decl))
    async with db.tenant_tx(ids["tenant_a"], actor_type="worker") as conn:
        await conn.execute("UPDATE declarations SET status = 'submitted', rs_receipt_number = 'DKL-1',"
                           " submitted_at = now() WHERE id = %s", (decl,))
        events = await (await conn.execute(
            "SELECT from_status, to_status, actor_type FROM declaration_events WHERE declaration_id = %s ORDER BY id",
            (decl,))).fetchall()
    assert [(e["from_status"], e["to_status"], e["actor_type"]) for e in events] == [
        (None, "draft_prepared", "worker"), ("draft_prepared", "approved", "user"), ("approved", "submitted", "worker")]


async def test_cross_tenant_references_are_rejected(db, ids):
    async with db.tenant_tx(ids["tenant_b"]) as conn:
        decl_b = (await (await conn.execute(
            "INSERT INTO declarations (tenant_id, taxpayer_id, type, period_start, period_end, due_date)"
            " VALUES (%s, %s, 'vat', '2026-09-01', '2026-09-30', '2026-10-15') RETURNING id",
            (ids["tenant_b"], ids["tp_b"]))).fetchone())["id"]
    async with db.tenant_tx(ids["tenant_a"]) as conn:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):  # FK-ის შემოწმება RLS-ს გვერდს უვლის
            await conn.execute("INSERT INTO automation_runs (tenant_id, taxpayer_id, kind, declaration_id)"
                               " VALUES (%s, %s, 'canary', %s)", (ids["tenant_a"], ids["tp_a"], decl_b))


async def _verify_audit(conn) -> int | None:
    return (await (await conn.execute("SELECT audit_log_verify() AS broken")).fetchone())["broken"]


async def test_audit_log_is_append_only_verifiable_hash_chain(db, ids, pg_dsn):
    tenant = ids["tenant_a"]
    async with db.tenant_tx(tenant) as conn:
        for action in ("a", "b", "c"):
            await audit(conn, tenant_id=tenant, actor_type="user", actor_id=ids["user"], action=action)
        rows = await (await conn.execute("SELECT seq, prev_hash, hash FROM audit_log ORDER BY seq")).fetchall()
        assert [r["seq"] for r in rows] == [1, 2, 3] and rows[1]["prev_hash"] == rows[0]["hash"]
        await conn.execute("SET LOCAL TimeZone = 'Pacific/Auckland'")  # ჰეში სესიის პარამეტრებს არ ეყრდნობა
        assert await _verify_audit(conn) is None
    async with db.tenant_tx(tenant) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await conn.execute("DELETE FROM audit_log")
    # superuser-ს შეუძლია ტრიგერების გამორთვა - ჯაჭვი ცვლილებასაც და წაშლასაც აჩენს
    with psycopg.connect(pg_dsn, autocommit=True) as c:
        c.execute("SET session_replication_role = replica")
        c.execute("UPDATE audit_log SET details = '{\"x\": 1}' WHERE tenant_id = %s AND seq = 3", (tenant,))
        assert c.execute("SELECT audit_log_verify(%s)", (tenant,)).fetchone()[0] == 3
        c.execute("DELETE FROM audit_log WHERE tenant_id = %s AND seq = 1", (tenant,))
        assert c.execute("SELECT audit_log_verify(%s)", (tenant,)).fetchone()[0] == 1


async def test_audit_chain_order_does_not_depend_on_id_order(db, ids):
    tenant = ids["tenant_a"]

    async def write(action):
        async with db.tenant_tx(tenant) as conn:
            await audit(conn, tenant_id=tenant, actor_type="system", actor_id=None, action=action)

    async with db.tenant_tx(tenant) as first:
        await first.execute("SELECT pg_advisory_xact_lock(hashtextextended('audit:' || %s::text, 0))", (str(tenant),))
        second = asyncio.create_task(write("second"))  # იღებს id-ს და ელოდება ჯაჭვის lock-ს
        for _ in range(100):
            waiting = await (await first.execute(
                "SELECT count(*) AS n FROM pg_locks WHERE locktype = 'advisory' AND NOT granted")).fetchone()
            if waiting["n"]:
                break
            await asyncio.sleep(0.05)
        await audit(first, tenant_id=tenant, actor_type="system", actor_id=None, action="first")
    await second
    async with db.tenant_tx(tenant) as conn:
        rows = await (await conn.execute("SELECT id, action FROM audit_log ORDER BY seq")).fetchall()
        assert [r["action"] for r in rows] == ["first", "second"] and rows[0]["id"] > rows[1]["id"]
        assert await _verify_audit(conn) is None


# ------------------------------------------------------------------ BYOK key flow


class FakeAdapter:
    def __init__(self, provider, valid_key="sk-ant-valid-key-123456"):
        self.provider = provider
        self.valid_key = valid_key

    async def list_models(self, api_key):
        if api_key != self.valid_key:
            raise LLMAuthError("bad key")
        return ["claude-opus-5", "claude-sonnet-5"]

    async def generate_json(self, **kw):
        raise AssertionError("not used")


@pytest.fixture
def box():
    return SecretBox(LocalKeyProvider({"k1": os.urandom(32)}, "k1"))


async def test_register_and_load_llm_key_is_tenant_bound(db, ids, box):
    router = LLMRouter({Provider.CLAUDE: FakeAdapter(Provider.CLAUDE)})
    view, models = await register_llm_key(db, router, box, tenant_id=ids["tenant_a"], user_id=ids["user"],
                                          provider=Provider.CLAUDE, api_key="sk-ant-valid-key-123456",
                                          model="claude-sonnet-5")
    assert view.key_hint == "…3456" and view.is_default and "claude-opus-5" in models
    creds = await load_default_credentials(db, box, ids["tenant_a"])
    assert creds.api_key == "sk-ant-valid-key-123456" and creds.model == "claude-sonnet-5"
    with pytest.raises(NoLlmCredentialError):
        await load_default_credentials(db, box, ids["tenant_b"])  # RLS: სხვა tenant ვერ ხედავს


async def test_first_llm_key_always_becomes_default(db, ids, box):
    router = LLMRouter({Provider.CLAUDE: FakeAdapter(Provider.CLAUDE)})
    kwargs = dict(tenant_id=ids["tenant_a"], user_id=ids["user"], provider=Provider.CLAUDE,
                  api_key="sk-ant-valid-key-123456", make_default=False)
    first, _ = await register_llm_key(db, router, box, **kwargs)
    second, _ = await register_llm_key(db, router, box, **kwargs)
    assert first.is_default and not second.is_default


async def test_api_llm_key_endpoints_and_otp(db, ids, box):
    app = create_app(with_lifespan=False)
    broker = InMemoryOtpBroker()
    app.dependency_overrides.update({
        get_db: lambda: db,
        get_router: lambda: LLMRouter({Provider.CLAUDE: FakeAdapter(Provider.CLAUDE)}),
        get_secret_box: lambda: box,
        get_otp_broker: lambda: broker,
    })
    headers = {"X-Tenant-Id": str(ids["tenant_a"]), "X-User-Id": str(ids["user"])}
    with TestClient(app) as client:
        bad = client.post("/v1/llm/credentials", headers=headers,
                          json={"provider": "claude", "api_key": "sk-ant-WRONG-000000"})
        assert bad.status_code == 400
        wrong_model = client.post("/v1/llm/credentials", headers=headers,
                                  json={"provider": "claude", "api_key": "sk-ant-valid-key-123456", "model": "gpt-6"})
        assert wrong_model.status_code == 422
        ok = client.post("/v1/llm/credentials", headers=headers,
                         json={"provider": "claude", "api_key": "sk-ant-valid-key-123456"})
        assert ok.status_code == 201, ok.text
        listed = client.get("/v1/llm/credentials", headers=headers).json()
        assert listed[0]["key_hint"] == "…3456" and "sk-ant" not in str(listed)
        assert client.post("/v1/rsge/otp/unknown", headers=headers, json={"code": "123456"}).status_code == 404
        cal = client.get("/v1/calendar/upcoming", headers=headers,
                         params={"regimes": ["vat_payer"], "today": "2026-09-27", "horizon_days": 30}).json()
        assert cal[0]["due_date"] == "2026-10-15"
        assert client.get("/v1/llm/credentials").status_code == 401
