"""PostgreSQL სქემა (RLS, ბალანსი, დადასტურება, აუდიტი) + API-ის გასაღების ნაკადი. სჭირდება TEST_DATABASE_URL."""

import json
import os
import time
import uuid

import jwt
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.api.deps import get_db, get_otp_broker, get_router, get_secret_box, get_token_verifier
from app.config import Settings, check_startup_settings, get_settings
from app.db.database import Database, audit
from app.llm import LLMAuthError, LLMRouter, Provider
from app.llm.base import RawCompletion, Usage
from app.main import create_app
from app.rsge.otp import InMemoryOtpBroker
from app.security.auth import OidcTokenVerifier
from app.security.crypto import LocalKeyProvider, SecretBox
from app.services.llm_keys import (
    NoLlmCredentialError,
    load_default_credentials,
    register_llm_key,
    revoke_llm_key,
    set_default_llm_key,
)
from tests.test_llm_router import VALID_DOC

pytestmark = pytest.mark.postgres


def seed(dsn: str) -> dict:
    ids = {k: uuid.uuid4() for k in ("tenant_a", "tenant_b", "user", "viewer", "tp_a", "tp_b")}
    with psycopg.connect(dsn, autocommit=True) as c:  # superuser - RLS-ს გვერდს უვლის (მხოლოდ seed)
        for u in ("user", "viewer"):
            c.execute("INSERT INTO users (id, email, auth_subject) VALUES (%s, %s, %s)",
                      (ids[u], f"{ids[u]}@example.ge", f"idp|{ids[u]}"))
        for t, tp, tin in (("tenant_a", "tp_a", "405123456"), ("tenant_b", "tp_b", "205000111")):
            c.execute("INSERT INTO tenants (id, name) VALUES (%s, %s)", (ids[t], t))
            c.execute("INSERT INTO taxpayers (id, tenant_id, tin, legal_name, legal_form, regimes)"
                      " VALUES (%s, %s, %s, %s, 'llc', '{vat_payer}')", (ids[tp], ids[t], tin, f"შპს {t}"))
            c.execute("INSERT INTO chart_of_accounts (tenant_id, code, name_ka, type) VALUES"
                      " (%s, '1210', 'ბანკი', 'asset'), (%s, '6110', 'შემოსავალი', 'revenue')", (ids[t], ids[t]))
        c.execute("INSERT INTO tenant_members (tenant_id, user_id, role) VALUES (%s, %s, 'owner'), (%s, %s, 'viewer')",
                  (ids["tenant_a"], ids["user"], ids["tenant_a"], ids["viewer"]))
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
        assert len(await (await conn.execute("SELECT id FROM users")).fetchall()) == 2  # owner + viewer
        with pytest.raises(psycopg.errors.InsufficientPrivilege):  # WITH CHECK: სხვის tenant_id-ით ჩაწერა
            await conn.execute("INSERT INTO taxpayers (tenant_id, tin, legal_name, legal_form)"
                               " VALUES (%s, '111222333', 'x', 'llc')", (ids["tenant_b"],))


async def test_users_visible_only_to_their_tenants(db, ids):
    async with db.tenant_tx(ids["tenant_b"]) as conn:
        assert await (await conn.execute("SELECT id FROM users")).fetchall() == []


async def test_posted_entry_must_balance_and_is_immutable(db, ids):
    async def post(debit, credit):
        async with db.tenant_tx(ids["tenant_a"]) as conn:  # draft -> სტრიქონები -> posted
            cur = await conn.execute(
                "INSERT INTO journal_entries (tenant_id, taxpayer_id, entry_date, description, status,"
                " created_by_type) VALUES (%s, %s, '2026-09-01', 'გაყიდვა', 'draft', 'user') RETURNING id",
                (ids["tenant_a"], ids["tp_a"]))
            entry = (await cur.fetchone())["id"]
            await conn.execute("INSERT INTO journal_lines (tenant_id, entry_id, account_code, debit) VALUES"
                               " (%s, %s, '1210', %s)", (ids["tenant_a"], entry, debit))
            await conn.execute("INSERT INTO journal_lines (tenant_id, entry_id, account_code, credit) VALUES"
                               " (%s, %s, '6110', %s)", (ids["tenant_a"], entry, credit))
            await conn.execute("UPDATE journal_entries SET status = 'posted', posted_at = now() WHERE id = %s",
                               (entry,))
        return entry

    with pytest.raises(psycopg.errors.CheckViolation):
        await post("100.00", "99.99")
    entry = await post("118.00", "118.00")

    async def attempt(statement, params=(entry,)):
        async with db.tenant_tx(ids["tenant_a"]) as conn:
            await conn.execute(statement, params)

    with pytest.raises(psycopg.errors.IntegrityConstraintViolation):
        await attempt("UPDATE journal_lines SET debit = 1 WHERE entry_id = %s AND debit > 0")
    with pytest.raises(psycopg.errors.IntegrityConstraintViolation):  # დაბალანსებული წყვილის "ჩამატება"
        await attempt("INSERT INTO journal_lines (tenant_id, entry_id, account_code, debit) VALUES (%s, %s, '1210', 5)",
                      (ids["tenant_a"], entry))
    with pytest.raises(psycopg.errors.IntegrityConstraintViolation):  # სხვა საგადასახადო პერიოდში გადატანა
        await attempt("UPDATE journal_entries SET entry_date = '2026-08-31' WHERE id = %s")
    with pytest.raises(psycopg.errors.IntegrityConstraintViolation):
        await attempt("DELETE FROM journal_entries WHERE id = %s")
    with pytest.raises(psycopg.errors.IntegrityConstraintViolation):
        await attempt("UPDATE journal_entries SET status = 'draft' WHERE id = %s")
    await attempt("UPDATE journal_entries SET status = 'reversed' WHERE id = %s")  # ერთადერთი დაშვებული


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
    async with db.tenant_tx(ids["tenant_a"], actor_type="worker") as conn:
        with pytest.raises(psycopg.errors.IntegrityConstraintViolation):  # წარდგენილი - უცვლელია
            await conn.execute("UPDATE declarations SET rs_receipt_number = 'X' WHERE id = %s", (decl,))
    async with db.tenant_tx(ids["tenant_a"], actor_type="worker") as conn:
        await conn.execute("UPDATE declarations SET receipt_screenshot_key = 's3://ev/1.png' WHERE id = %s", (decl,))


async def test_only_the_approving_staff_user_can_approve(db, ids):
    async with db.tenant_tx(ids["tenant_a"], actor_type="worker") as conn:
        cur = await conn.execute(
            "INSERT INTO declarations (tenant_id, taxpayer_id, type, period_start, period_end, due_date,"
            " status, is_zero, form_fingerprint) VALUES (%s, %s, 'vat', '2026-10-01', '2026-10-31', '2026-11-16',"
            " 'draft_prepared', true, 'fp') RETURNING id", (ids["tenant_a"], ids["tp_a"]))
        decl = (await cur.fetchone())["id"]
    approve = ("UPDATE declarations SET status = 'approved', approved_by = %s, approved_at = now(),"
               " approved_fingerprint = 'fp' WHERE id = %s")

    async def attempt(actor_type, actor_id, approver):
        async with db.tenant_tx(ids["tenant_a"], actor_type=actor_type, actor_id=actor_id) as conn:
            await conn.execute(approve, (approver, decl))

    with pytest.raises(psycopg.errors.InsufficientPrivilege):  # worker-ი მომხმარებლის სახელით
        await attempt("worker", None, ids["user"])
    with pytest.raises(psycopg.errors.InsufficientPrivilege):  # სხვის სახელით
        await attempt("user", ids["viewer"], ids["user"])
    with pytest.raises(psycopg.errors.InsufficientPrivilege):  # viewer-ს დადასტურების უფლება არ აქვს
        await attempt("user", ids["viewer"], ids["viewer"])
    with pytest.raises(psycopg.errors.InsufficientPrivilege):  # auto_submit_zero თანხმობის გარეშე
        await attempt("system", None, ids["user"])
    async with db.tenant_tx(ids["tenant_a"]) as conn:
        await conn.execute("UPDATE tenants SET settings = settings || '{\"auto_submit_zero\": true}'")
    await attempt("system", None, ids["user"])  # წესით დადასტურება: მფლობელის თანხმობა + ნულოვანი


async def test_audit_log_is_append_only_hash_chain(db, ids, pg_dsn):
    async with db.tenant_tx(ids["tenant_a"]) as conn:
        for action in ("a", "b", "c"):
            await audit(conn, tenant_id=ids["tenant_a"], actor_type="user", actor_id=ids["user"], action=action,
                        entity_type=None if action == "b" else "doc", entity_id="x", details={"n": action})
        rows = await (await conn.execute("SELECT seq, prev_hash, hash FROM audit_log ORDER BY seq")).fetchall()
        verified = await (await conn.execute("SELECT * FROM audit_log_verify(%s)", (ids["tenant_a"],))).fetchone()
    assert [r["seq"] for r in rows] == [1, 2, 3] and rows[1]["prev_hash"] == rows[0]["hash"]
    assert verified["ok"] and verified["checked"] == 3 and verified["last_hash"] == rows[-1]["hash"]
    async with db.tenant_tx(ids["tenant_a"]) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await conn.execute("DELETE FROM audit_log")

    with psycopg.connect(pg_dsn, autocommit=True) as c:  # superuser (DBA / თავდამსხმელი DB-ის წვდომით)
        c.execute("SET TimeZone = 'Asia/Tbilisi'")  # შემოწმება სესიის დროის სარტყელზე არ არის დამოკიდებული
        assert c.execute("SELECT ok FROM audit_log_verify(%s)", (ids["tenant_a"],)).fetchone()[0]
        c.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_log_append_only")
        # entity_type/entity_id-ის "გადანაცვლება" - ძველ (concat_ws) ჰეშს ეს ვერ ამჩნევდა
        c.execute("UPDATE audit_log SET entity_type = 'x', entity_id = NULL WHERE tenant_id = %s AND seq = 2",
                  (ids["tenant_a"],))
        c.execute("ALTER TABLE audit_log ENABLE TRIGGER audit_log_append_only")
        broken = c.execute("SELECT ok, first_broken_seq FROM audit_log_verify(%s)", (ids["tenant_a"],)).fetchone()
    assert broken == (False, 2)


async def test_audit_chain_stays_valid_under_concurrent_writers(db, ids):
    import asyncio

    async def write(i):
        async with db.tenant_tx(ids["tenant_a"]) as conn:
            await audit(conn, tenant_id=ids["tenant_a"], actor_type="worker", actor_id=None, action=f"job.{i}")
            await asyncio.sleep(0.01)  # ტრანზაქცია ღიაა, სანამ სხვები ლოკს ელოდებიან

    await asyncio.gather(*(write(i) for i in range(8)))
    async with db.tenant_tx(ids["tenant_a"]) as conn:
        verified = await (await conn.execute("SELECT * FROM audit_log_verify(%s)", (ids["tenant_a"],))).fetchone()
    assert verified["ok"] and verified["checked"] == 8


# ------------------------------------------------------------------ BYOK key flow


class FakeAdapter:
    def __init__(self, provider, valid_key="sk-ant-valid-key-123456"):
        self.provider = provider
        self.valid_key = valid_key
        self.calls = 0

    async def list_models(self, api_key):
        if api_key != self.valid_key:
            raise LLMAuthError("bad key")
        return ["claude-opus-5", "claude-sonnet-5"]

    async def generate_json(self, **kw):
        assert kw["api_key"] == self.valid_key  # worker-მა გაშიფრა სწორი გასაღები
        self.calls += 1
        return RawCompletion(json.dumps(VALID_DOC), kw["model"], Usage(10, 10))


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
        assert client.post("/v1/rsge/otp/" + "0" * 32, headers=headers, json={"code": "123456"}).status_code == 404
        assert client.post("/v1/rsge/otp/../x", headers=headers, json={"code": "123456"}).status_code in (404, 422)
        cal = client.get("/v1/calendar/upcoming", headers=headers,
                         params={"regimes": ["vat_payer"], "today": "2026-09-27", "horizon_days": 30}).json()
        assert cal[0]["due_date"] == "2026-10-15"
        assert client.get("/v1/llm/credentials").status_code == 401


# ------------------------------------------------------------------ key lifecycle


async def test_first_key_is_default_and_revocation_shreds_ciphertext(db, ids, box, pg_dsn):
    router = LLMRouter({Provider.CLAUDE: FakeAdapter(Provider.CLAUDE)})
    kw = dict(tenant_id=ids["tenant_a"], user_id=ids["user"], provider=Provider.CLAUDE,
              api_key="sk-ant-valid-key-123456")
    first, _ = await register_llm_key(db, router, box, make_default=False, **kw)
    assert first.is_default  # პირველი გასაღები ყოველთვის ნაგულისხმევია, სხვაგვარად დამუშავება ვერ დაიწყება
    second, _ = await register_llm_key(db, router, box, make_default=False, model="claude-sonnet-5", **kw)
    assert not second.is_default

    await set_default_llm_key(db, tenant_id=ids["tenant_a"], user_id=ids["user"], credential_id=second.id)
    assert (await load_default_credentials(db, box, ids["tenant_a"])).model == "claude-sonnet-5"

    await revoke_llm_key(db, tenant_id=ids["tenant_a"], user_id=ids["user"], credential_id=second.id)
    with pytest.raises(NoLlmCredentialError):  # სხვა გასაღები ავტომატურად არ ხდება ნაგულისხმევი
        await load_default_credentials(db, box, ids["tenant_a"])
    with pytest.raises(NoLlmCredentialError):
        await revoke_llm_key(db, tenant_id=ids["tenant_a"], user_id=ids["user"], credential_id=second.id)
    with psycopg.connect(pg_dsn) as c:
        row = c.execute("SELECT status, length(ciphertext), length(wrapped_dek) FROM llm_credentials WHERE id = %s",
                        (second.id,)).fetchone()
    assert row == ("revoked", 0, 0)


# ------------------------------------------------------------------ API: roles, documents


def _api(db, box, adapter=None):
    app = create_app(with_lifespan=False)
    app.dependency_overrides.update({
        get_db: lambda: db,
        get_router: lambda: LLMRouter({Provider.CLAUDE: adapter or FakeAdapter(Provider.CLAUDE)}),
        get_secret_box: lambda: box,
        get_otp_broker: lambda: InMemoryOtpBroker(),
    })
    return app


def test_roles_come_from_the_database_not_the_client(db, ids, box):
    with TestClient(_api(db, box)) as client:
        viewer = {"X-Tenant-Id": str(ids["tenant_a"]), "X-User-Id": str(ids["viewer"])}
        assert client.get("/v1/llm/credentials", headers=viewer).status_code == 200
        assert client.post("/v1/llm/credentials", headers=viewer,
                           json={"provider": "claude", "api_key": "sk-ant-valid-key-123456"}).status_code == 403
        # tenant_b-ის წევრი არ არის -> 403, ხოლო უცნობი მომხმარებელი -> იგივე 403 (განსხვავებას არ ვამჟღავნებთ)
        other = {"X-Tenant-Id": str(ids["tenant_b"]), "X-User-Id": str(ids["user"])}
        assert client.get("/v1/llm/credentials", headers=other).status_code == 403
        ghost = {"X-Tenant-Id": str(ids["tenant_a"]), "X-User-Id": str(uuid.uuid4())}
        assert client.get("/v1/llm/credentials", headers=ghost).status_code == 403


def test_document_extraction_endpoint(db, ids, box):
    adapter = FakeAdapter(Provider.CLAUDE)
    headers = {"X-Tenant-Id": str(ids["tenant_a"]), "X-User-Id": str(ids["user"])}
    with TestClient(_api(db, box, adapter)) as client:
        pdf = {"file": ("ინვოისი.pdf", b"%PDF-1.7 test", "application/pdf")}
        assert client.post("/v1/documents/extract", headers=headers, params={"our_tin": "405123456"},
                           files=pdf).status_code == 409  # LLM გასაღები ჯერ არ არის
        client.post("/v1/llm/credentials", headers=headers,
                    json={"provider": "claude", "api_key": "sk-ant-valid-key-123456"}).raise_for_status()
        # სხვა კომპანიის (tenant_b) საიდ. კოდით ვალიდაციის გაყალბება არ შეიძლება
        assert client.post("/v1/documents/extract", headers=headers, params={"our_tin": "205000111"},
                           files=pdf).status_code == 404
        ok = client.post("/v1/documents/extract", headers=headers, params={"our_tin": "405123456"}, files=pdf)
        assert ok.status_code == 200, ok.text
        body = ok.json()
        assert body["extraction"]["total_amount"] == "118.00" and body["needs_review"] is False
        assert adapter.calls == 1
        exe = {"file": ("x.exe", b"MZ\x90\x00", "application/octet-stream")}
        assert client.post("/v1/documents/extract", headers=headers, params={"our_tin": "405123456"},
                           files=exe).status_code == 415


# ------------------------------------------------------------------ OIDC


class _StaticKey:
    def __init__(self, public_key):
        self.key = public_key

    def get_signing_key_from_jwt(self, token):
        return self


@pytest.fixture
def oidc(monkeypatch):
    monkeypatch.setenv("APP_AUTH_MODE", "oidc")
    get_settings.cache_clear()
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = OidcTokenVerifier(issuer="https://idp.example.ge", audience="ai-accountant",
                                 key_resolver=_StaticKey(private.public_key()))

    def token(subject, **overrides):
        now = int(time.time())
        claims = {"iss": "https://idp.example.ge", "aud": "ai-accountant", "sub": subject, "iat": now,
                  "exp": now + 300, **overrides}
        return jwt.encode(claims, private, algorithm="RS256", headers={"kid": "k1"})

    return verifier, token


def test_oidc_bearer_token_and_membership(db, ids, box, oidc):
    verifier, token = oidc
    app = _api(db, box)
    app.dependency_overrides[get_token_verifier] = lambda: verifier
    tenant = {"X-Tenant-Id": str(ids["tenant_a"])}
    with TestClient(app) as client:
        def get(tok, **h):
            return client.get("/v1/llm/credentials", headers={**tenant, **h, "Authorization": f"Bearer {tok}"})

        assert get(token(f"idp|{ids['user']}")).status_code == 200
        # dev header OIDC რეჟიმში იგნორირდება: იდენტობა მხოლოდ token-იდან
        assert client.get("/v1/llm/credentials", headers={**tenant, "X-User-Id": str(ids["user"])}).status_code == 401
        assert get(token(f"idp|{ids['user']}", aud="other-app")).status_code == 401
        assert get(token(f"idp|{ids['user']}", exp=int(time.time()) - 3600)).status_code == 401
        assert get(token("idp|stranger")).status_code == 403
        forged = jwt.encode({"iss": "https://idp.example.ge", "aud": "ai-accountant", "sub": f"idp|{ids['user']}",
                             "iat": int(time.time()), "exp": int(time.time()) + 60}, "s" * 32, algorithm="HS256")
        assert get(forged).status_code == 401  # alg confusion: HS256 აკრძალულია


def test_oidc_mfa_required_for_staff(db, ids, box, oidc, monkeypatch):
    verifier, token = oidc
    monkeypatch.setenv("APP_OIDC_REQUIRE_MFA_FOR_STAFF", "true")
    get_settings.cache_clear()
    app = _api(db, box)
    app.dependency_overrides[get_token_verifier] = lambda: verifier
    tenant = {"X-Tenant-Id": str(ids["tenant_a"])}
    with TestClient(app) as client:
        def get(tok):
            return client.get("/v1/llm/credentials", headers={**tenant, "Authorization": f"Bearer {tok}"})

        assert get(token(f"idp|{ids['user']}", amr=["pwd"])).status_code == 403  # owner, MFA-ს გარეშე
        assert get(token(f"idp|{ids['user']}", amr=["pwd", "mfa"])).status_code == 200
        assert get(token(f"idp|{ids['viewer']}", amr=["pwd"])).status_code == 200  # viewer - მხოლოდ კითხვა


def test_dev_headers_are_refused_outside_dev():
    with pytest.raises(RuntimeError, match="dev_headers"):
        check_startup_settings(Settings(_env_file=None, env="prod", auth_mode="dev_headers"))
    check_startup_settings(Settings(_env_file=None, env="prod", auth_mode="oidc"))
