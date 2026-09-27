"""API v1 ინტეგრაციული ტესტები PostgreSQL-ზე: OIDC + MFA, თანხმობები, BYOK სასიცოცხლო ციკლი, LLM ხარჯის
აღრიცხვა, დეკლარაციის დადასტურება, აუდიტის შემოწმება. სჭირდება TEST_DATABASE_URL."""

import hashlib
import json
import time
import uuid

import jwt
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.api.deps import get_db, get_oidc_verifier, get_otp_broker, get_router, get_secret_box
from app.config import Settings, get_settings
from app.db.database import Database
from app.llm import LLMRouter, LLMUnavailableError, Provider
from app.llm.base import RawCompletion, Usage
from app.main import create_app
from app.rsge.otp import InMemoryOtpBroker
from app.security.crypto import LocalKeyProvider, SecretBox
from app.security.oidc import OidcVerifier
from tests.test_llm_router import VALID_DOC
from tests.test_oidc import AUDIENCE, ISSUER, StaticJwks

pytestmark = pytest.mark.postgres

OUR_TIN = "405123456"
FINGERPRINT = hashlib.sha256(b"form-v1").hexdigest()


class FakeLlm:
    provider = Provider.CLAUDE

    def __init__(self):
        self.fail_with: Exception | None = None

    async def list_models(self, api_key):
        return ["claude-opus-5", "claude-sonnet-5"]

    async def generate_json(self, **kw):
        if self.fail_with:
            raise self.fail_with
        return RawCompletion(json.dumps(VALID_DOC), kw["model"], Usage(1500, 400), request_id="req_1")


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
async def db(pg_dsn):
    database = Database(pg_dsn, set_role="ai_accountant_app")
    await database.open()
    yield database
    await database.close()


@pytest.fixture
def world(pg_dsn):
    """ერთი კომპანია: owner, accountant, viewer (OIDC subject-ებით) + გარეშე პირი."""
    w = {k: uuid.uuid4() for k in ("tenant", "taxpayer", "owner", "accountant", "viewer", "outsider")}
    with psycopg.connect(pg_dsn, autocommit=True) as c:
        c.execute("INSERT INTO tenants (id, name) VALUES (%s, 'შპს ტესტი')", (w["tenant"],))
        c.execute("INSERT INTO taxpayers (id, tenant_id, tin, legal_name, legal_form, regimes)"
                  " VALUES (%s, %s, %s, 'შპს ტესტი', 'llc', '{vat_payer}')", (w["taxpayer"], w["tenant"], OUR_TIN))
        for who in ("owner", "accountant", "viewer", "outsider"):
            c.execute("INSERT INTO users (id, email, auth_subject) VALUES (%s, %s, %s)",
                      (w[who], f"{w[who]}@example.ge", f"sub-{w[who]}"))
        for who in ("owner", "accountant", "viewer"):
            c.execute("INSERT INTO tenant_members (tenant_id, user_id, role) VALUES (%s, %s, %s)",
                      (w["tenant"], w[who], who))
    return w


@pytest.fixture
def llm():
    return FakeLlm()


@pytest.fixture
def client(db, llm, rsa_key):
    settings = Settings(_env_file=None, auth_mode="oidc", oidc_issuer=ISSUER, oidc_audience=AUDIENCE,
                        oidc_jwks_url="https://id.example.ge/jwks", oidc_algorithms=["RS256"])
    app = create_app(with_lifespan=False, settings=settings)
    verifier = OidcVerifier(issuer=ISSUER, audience=AUDIENCE, algorithms=["RS256"],
                            jwks_client=StaticJwks(rsa_key.public_key()))
    box = SecretBox(LocalKeyProvider({"k1": b"k" * 32}, "k1"))
    app.dependency_overrides.update({
        get_settings: lambda: settings, get_oidc_verifier: lambda: verifier, get_db: lambda: db,
        get_router: lambda: LLMRouter({Provider.CLAUDE: llm}, transient_retries=0),
        get_secret_box: lambda: box, get_otp_broker: lambda: InMemoryOtpBroker(),
    })
    with TestClient(app) as c:
        yield c


def auth(key, world, who: str, *, mfa: bool = True, tenant=None) -> dict:
    now = int(time.time())
    token = jwt.encode({"iss": ISSUER, "aud": AUDIENCE, "sub": f"sub-{world[who]}", "iat": now, "exp": now + 300,
                        "amr": ["pwd", "mfa"] if mfa else ["pwd"]}, key, algorithm="RS256")
    return {"Authorization": f"Bearer {token}", "X-Tenant-Id": str(tenant or world["tenant"])}


# ------------------------------------------------------------------ authentication


def test_oidc_resolves_role_from_membership_and_enforces_mfa(client, world, rsa_key):
    me = client.get("/v1/me", headers=auth(rsa_key, world, "owner"))
    assert me.status_code == 200 and me.json() == {"tenant_id": str(world["tenant"]),
                                                   "user_id": str(world["owner"]), "role": "owner"}
    assert me.headers["x-request-id"]

    no_mfa = client.get("/v1/me", headers=auth(rsa_key, world, "owner", mfa=False))
    assert no_mfa.status_code == 403 and "multi-factor" in no_mfa.json()["detail"]
    # viewer-ს (მხოლოდ კითხვა) MFA არ სჭირდება, მაგრამ ცვლილებას ვერ აკეთებს
    assert client.get("/v1/me", headers=auth(rsa_key, world, "viewer", mfa=False)).json()["role"] == "viewer"
    assert client.post("/v1/consents", headers=auth(rsa_key, world, "viewer"),
                       json={"kind": "ai_processing", "document_version": "v1"}).status_code == 403

    assert client.get("/v1/me", headers=auth(rsa_key, world, "outsider")).status_code == 403
    assert client.get("/v1/me", headers=auth(rsa_key, world, "owner", tenant=uuid.uuid4())).status_code == 403
    missing_tenant = auth(rsa_key, world, "owner")
    del missing_tenant["X-Tenant-Id"]
    assert client.get("/v1/me", headers=missing_tenant).status_code == 400
    anonymous = client.get("/v1/me")
    assert anonymous.status_code == 401 and anonymous.headers["www-authenticate"].startswith("Bearer")
    # dev header-ები OIDC რეჟიმში არაფერს ნიშნავს
    assert client.get("/v1/me", headers={"X-Tenant-Id": str(world["tenant"]),
                                         "X-User-Id": str(world["owner"])}).status_code == 401


# ------------------------------------------------------------------ consents, extraction, usage


def test_extraction_requires_ai_consent_and_is_metered(client, world, rsa_key, llm):
    owner, accountant = auth(rsa_key, world, "owner"), auth(rsa_key, world, "accountant")
    assert client.post("/v1/llm/credentials", headers=owner,
                       json={"provider": "claude", "api_key": "sk-ant-valid-key-123456"}).status_code == 201

    def extract():
        return client.post("/v1/documents/extract", headers=accountant, params={"our_tin": OUR_TIN},
                           files={"file": ("invoice.pdf", b"%PDF-1.7 fake invoice", "application/pdf")})

    blocked = extract()
    assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "consent_required"

    granted = client.post("/v1/consents", headers=owner, json={"kind": "ai_processing", "document_version": "2026-09"})
    assert granted.status_code == 201
    again = client.post("/v1/consents", headers=owner, json={"kind": "ai_processing", "document_version": "2026-09"})
    assert again.json()["id"] == granted.json()["id"]  # idempotent
    assert [c["kind"] for c in client.get("/v1/consents", headers=accountant).json()] == ["ai_processing"]

    ok = extract()
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["extraction"]["total_amount"] == "118.00" and not body["needs_review"]
    assert uuid.UUID(body["llm"]["call_id"])

    llm.fail_with = LLMUnavailableError("overloaded")
    assert extract().status_code == 502

    usage = client.get("/v1/llm/usage", headers=accountant, params={"since": "2000-01-01"}).json()
    assert usage == [{"provider": "claude", "model": "claude-opus-5", "purpose": "extract_document", "calls": 2,
                      "failed_calls": 1, "input_tokens": 1500, "output_tokens": 400}]

    assert client.delete("/v1/consents/ai_processing", headers=owner).status_code == 204
    llm.fail_with = None
    assert extract().status_code == 409
    assert client.delete("/v1/consents/ai_processing", headers=owner).status_code == 404


# ------------------------------------------------------------------ BYOK lifecycle


def test_llm_key_default_switch_and_crypto_shredding_revoke(client, world, rsa_key, pg_dsn):
    owner = auth(rsa_key, world, "owner")
    first = client.post("/v1/llm/credentials", headers=owner,
                        json={"provider": "claude", "api_key": "sk-ant-first-key-111111"}).json()
    second = client.post("/v1/llm/credentials", headers=owner,
                         json={"provider": "claude", "api_key": "sk-ant-second-key-222222", "make_default": False,
                               "model": "claude-sonnet-5"}).json()
    assert first["is_default"] and not second["is_default"]

    switched = client.post(f"/v1/llm/credentials/{second['id']}/default", headers=owner)
    assert switched.status_code == 200 and switched.json()["is_default"]
    listed = {k["id"]: k["is_default"] for k in client.get("/v1/llm/credentials", headers=owner).json()}
    assert listed == {first["id"]: False, second["id"]: True}

    assert client.delete(f"/v1/llm/credentials/{second['id']}", headers=owner).status_code == 204
    assert [k["id"] for k in client.get("/v1/llm/credentials", headers=owner).json()] == [first["id"]]
    assert client.delete(f"/v1/llm/credentials/{second['id']}", headers=owner).status_code == 404
    assert client.post(f"/v1/llm/credentials/{second['id']}/default", headers=owner).status_code == 404
    with psycopg.connect(pg_dsn) as c:
        row = c.execute("SELECT status, is_default, octet_length(ciphertext) AS ct, octet_length(wrapped_dek) AS dek"
                        " FROM llm_credentials WHERE id = %s", (second["id"],)).fetchone()
        actions = [r[0] for r in c.execute("SELECT action FROM audit_log WHERE tenant_id = %s ORDER BY seq",
                                           (world["tenant"],))]
    assert row == ("revoked", False, 0, 0)
    assert actions == ["llm_key.created", "llm_key.created", "llm_key.set_default", "llm_key.revoked"]
    # accountant-ს გასაღებების მართვა არ შეუძლია
    assert client.delete(f"/v1/llm/credentials/{first['id']}",
                         headers=auth(rsa_key, world, "accountant")).status_code == 403


# ------------------------------------------------------------------ declarations


def _prepared_declaration(pg_dsn, world) -> str:
    with psycopg.connect(pg_dsn, autocommit=True) as c:
        return str(c.execute(
            "INSERT INTO declarations (tenant_id, taxpayer_id, type, period_start, period_end, due_date, status,"
            " is_zero, form_values, form_fingerprint) VALUES (%s, %s, 'vat', '2026-09-01', '2026-09-30',"
            " '2026-10-15', 'draft_prepared', true, '{\"line_1\": \"0\"}', %s) RETURNING id",
            (world["tenant"], world["taxpayer"], FINGERPRINT)).fetchone()[0])


def test_declaration_approval_binds_to_the_viewed_form(client, world, rsa_key, pg_dsn):
    decl = _prepared_declaration(pg_dsn, world)
    accountant, viewer = auth(rsa_key, world, "accountant"), auth(rsa_key, world, "viewer")

    listed = client.get("/v1/declarations", headers=viewer, params={"status": ["draft_prepared"]}).json()
    assert [d["id"] for d in listed] == [decl] and listed[0]["form_fingerprint"] == FINGERPRINT
    assert client.get("/v1/declarations", headers=viewer, params={"status": ["submitted"]}).json() == []

    assert client.post(f"/v1/declarations/{decl}/approve", headers=viewer,
                       json={"fingerprint": FINGERPRINT}).status_code == 403
    stale = client.post(f"/v1/declarations/{decl}/approve", headers=accountant,
                        json={"fingerprint": hashlib.sha256(b"form-v0").hexdigest()})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "form_changed"

    approved = client.post(f"/v1/declarations/{decl}/approve", headers=accountant, json={"fingerprint": FINGERPRINT})
    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert body["status"] == "approved" and body["approved_by"] == str(world["accountant"])
    assert [(e["from_status"], e["to_status"], e["actor_type"]) for e in body["events"]] == [
        (None, "draft_prepared", "system"), ("draft_prepared", "approved", "user")]

    again = client.post(f"/v1/declarations/{decl}/approve", headers=accountant, json={"fingerprint": FINGERPRINT})
    assert again.status_code == 409 and again.json()["detail"]["code"] == "not_approvable"
    assert client.get(f"/v1/declarations/{uuid.uuid4()}", headers=viewer).status_code == 404
    assert client.post(f"/v1/declarations/{decl}/approve", headers=accountant,
                       json={"fingerprint": "not-hex"}).status_code == 422


# ------------------------------------------------------------------ audit + ops


def test_audit_verify_endpoint(client, world, rsa_key):
    owner = auth(rsa_key, world, "owner")
    client.post("/v1/consents", headers=owner, json={"kind": "terms_of_service", "document_version": "1"})
    result = client.get("/v1/audit/verify", headers=owner).json()
    assert result["ok"] and result["checked"] == 1 and len(result["last_hash"]) == 64
    assert client.get("/v1/audit/verify", headers=auth(rsa_key, world, "accountant")).status_code == 403


class FakeRedis:
    def __init__(self, healthy: bool):
        self.healthy = healthy

    async def ping(self):
        if not self.healthy:
            raise ConnectionError("down")
        return True


@pytest.mark.parametrize("redis_ok,expected", [(True, 200), (False, 503)])
def test_readiness_checks_dependencies(db, redis_ok, expected):
    app = create_app(with_lifespan=False)
    app.state.db, app.state.redis = db, FakeRedis(redis_ok)
    with TestClient(app) as c:
        resp = c.get("/ready")
    assert resp.status_code == expected
    assert resp.json()["checks"] == {"database": "ok", "redis": "ok" if redis_ok else "unavailable"}
