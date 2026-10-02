"""E2E: API -> worker ნაკადი -> ნამდვილი Chromium ყალბ rs.ge პორტალზე -> PostgreSQL, SMS კოდი Redis-ით.

"მომხმარებელი" იქცევა ისე, როგორც ფრონტენდი: `GET /v1/rsge/otp/pending`-ში ხედავს მოთხოვნას და კოდს
`POST /v1/rsge/otp/{id}`-ით აგზავნის; ფორმას ადასტურებს `POST /approve`-ით ნანახი fingerprint-ით.
"""

import asyncio
import os
import uuid
from datetime import date
from pathlib import Path

import httpx
import psycopg
import pytest

from app.api.deps import get_db, get_jobs, get_otp_broker, get_secret_box
from app.config import Settings
from app.db.database import Database
from app.main import create_app
from app.rsge.otp import RedisOtpBroker
from app.security.crypto import LocalKeyProvider, SecretBox
from app.services.planning import plan_all_tenants
from app.workflows.declaration_flow import FlowContext, FlowError, prepare_declaration, submit_declaration
from tests.fake_rsge.server import FakePortal, FakePortalState
from tests.test_api_v1 import RecordingJobs
from tests.test_rsge_browser import CHROMIUM

TIN = "405123456"


@pytest.fixture
async def db(pg_dsn):
    database = Database(pg_dsn, set_role="ai_accountant_app")
    await database.open()
    yield database
    await database.close()


@pytest.fixture
def people(pg_dsn):
    p = {k: uuid.uuid4() for k in ("tenant", "owner", "accountant")}
    with psycopg.connect(pg_dsn, autocommit=True) as c:
        c.execute("INSERT INTO tenants (id, name) VALUES (%s, 'შპს ნული')", (p["tenant"],))
        for who in ("owner", "accountant"):
            c.execute("INSERT INTO users (id, email) VALUES (%s, %s)", (p[who], f"{p[who]}@example.ge"))
            c.execute("INSERT INTO tenant_members (tenant_id, user_id, role) VALUES (%s, %s, %s)",
                      (p["tenant"], p[who], who))
    return p


@pytest.fixture
async def redis_client():
    url = os.environ.get("TEST_REDIS_URL")
    if not url:
        pytest.skip("TEST_REDIS_URL not set")
    import redis.asyncio as redis

    client = redis.from_url(url)
    yield client
    await client.aclose()


class World:
    """API კლიენტი + worker-ის კონტექსტი ერთ event loop-ში, ყალბ პორტალთან."""

    def __init__(self, db, people, redis_client, portal: FakePortal, evidence: Path):
        self.people, self.portal, self.jobs = people, portal, RecordingJobs()
        box = SecretBox(LocalKeyProvider({"k1": b"\x01" * 32}, "k1"))
        prefix = f"otp-{uuid.uuid4().hex[:6]}"
        settings = Settings(_env_file=None, rsge_base_url=portal.base_url, evidence_dir=str(evidence),
                            rsge_chromium_executable=CHROMIUM, rsge_otp_timeout_seconds=20)
        self.flow = FlowContext(db=db, box=box, settings=settings, browser_timeout_ms=5_000,
                                otp_factory=lambda notifier: RedisOtpBroker(redis_client, notifier=notifier,
                                                                            prefix=prefix))
        app = create_app(with_lifespan=False)
        app.dependency_overrides.update({
            get_db: lambda: db, get_secret_box: lambda: box, get_jobs: lambda: self.jobs,
            get_otp_broker: lambda: RedisOtpBroker(redis_client, prefix=prefix),
        })
        self.api = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api")

    def headers(self, who: str) -> dict:
        return {"X-Tenant-Id": str(self.people["tenant"]), "X-User-Id": str(self.people[who]), "X-Role": who}

    async def setup_taxpayer(self, *, password="correct-horse", mandate=True) -> str:
        owner = self.headers("owner")
        if mandate:
            r = await self.api.post("/v1/consents", headers=owner,
                                    json={"kind": "rsge_automation_mandate", "document_version": "2026-09"})
            assert r.status_code == 201, r.text
        tp = await self.api.post("/v1/taxpayers", headers=owner, json={
            "tin": TIN, "legal_name": "შპს ნული", "legal_form": "llc", "regimes": ["vat_payer"]})
        assert tp.status_code == 201, tp.text
        cred = await self.api.put(f"/v1/taxpayers/{tp.json()['id']}/rsge-credentials/portal_user", headers=owner,
                                  json={"username": TIN, "password": password})
        assert cred.status_code == 200 and cred.json()["username_hint"] == "405***56"
        assert "correct-horse" not in cred.text
        return tp.json()["id"]

    async def new_declaration(self, taxpayer_id: str) -> str:
        r = await self.api.post("/v1/declarations", headers=self.headers("accountant"),
                                json={"taxpayer_id": taxpayer_id, "type": "vat", "period": "2026-09"})
        assert r.status_code == 201, r.text
        assert r.json()["status"] == "planned" and r.json()["due_date"] == "2026-10-15"
        return r.json()["id"]

    async def answer_sms_like_a_user(self, code: str) -> None:
        for _ in range(400):
            pending = (await self.api.get("/v1/rsge/otp/pending", headers=self.headers("accountant"))).json()
            if pending:
                assert pending[0]["phone_hint"] == "***-**-12" and pending[0]["run_kind"] == "prepare_zero_declaration"
                r = await self.api.post(f"/v1/rsge/otp/{pending[0]['id']}", headers=self.headers("accountant"),
                                        json={"code": code})
                assert r.status_code == 204, r.text
                return
            await asyncio.sleep(0.05)
        raise AssertionError("worker never asked for the SMS code")

    async def declaration(self, decl: str) -> dict:
        return (await self.api.get(f"/v1/declarations/{decl}", headers=self.headers("accountant"))).json()


@pytest.fixture
def portal_state():
    return FakePortalState()


@pytest.fixture
async def world(db, people, redis_client, portal_state, tmp_path):
    with FakePortal(portal_state) as portal:
        w = World(db, people, redis_client, portal, tmp_path / "evidence")
        yield w
        await w.api.aclose()


def rows(pg_dsn, query, *params):
    with psycopg.connect(pg_dsn) as c:
        return c.execute(query, params).fetchall()


# ------------------------------------------------------------------ happy path


@pytest.mark.browser
@pytest.mark.redis
async def test_zero_vat_end_to_end_with_sms_approval_and_session_reuse(world, portal_state, pg_dsn, tmp_path):
    tenant = world.people["tenant"]
    decl = await world.new_declaration(await world.setup_taxpayer())

    first = await world.api.post(f"/v1/declarations/{decl}/prepare", headers=world.headers("accountant"))
    again = await world.api.post(f"/v1/declarations/{decl}/prepare", headers=world.headers("accountant"))
    assert first.status_code == 202 and first.json()["enqueued"] and not again.json()["enqueued"]  # ორმაგი დაჭერა

    status, _ = await asyncio.gather(prepare_declaration(world.flow, tenant, uuid.UUID(decl)),
                                     world.answer_sms_like_a_user(portal_state.otp_code))
    assert status == "awaiting_approval"
    d = await world.declaration(decl)
    assert d["status"] == "awaiting_approval" and set(d["form_values"].values()) == {"0"}
    assert (tmp_path / "evidence" / d["draft_screenshot_key"]).exists()
    assert [(e["from_status"], e["to_status"]) for e in d["events"]] == [
        (None, "planned"), ("planned", "draft_prepared"), ("draft_prepared", "awaiting_approval")]
    cred = (await world.api.get(f"/v1/taxpayers/{d['taxpayer_id']}/rsge-credentials",
                                headers=world.headers("owner"))).json()[0]
    assert cred["has_session"] and cred["last_login_at"]  # სესია დაშიფრულად შეინახა
    assert rows(pg_dsn, "SELECT status::text, answered_by FROM otp_challenges WHERE tenant_id = %s", tenant) == [
        ("answered", world.people["accountant"])]

    approved = await world.api.post(f"/v1/declarations/{decl}/approve", headers=world.headers("owner"),
                                    json={"fingerprint": d["form_fingerprint"]})
    assert approved.status_code == 200 and world.jobs.jobs[approved.json()["submission_job_id"]][0] == \
        "submit_declaration"

    assert await submit_declaration(world.flow, tenant, uuid.UUID(decl)) == "submitted"
    d = await world.declaration(decl)
    assert d["status"] == "submitted" and d["rs_receipt_number"] == "DKL-2026-000001"
    assert [e["to_status"] for e in d["events"]][-3:] == ["approved", "submitting", "submitted"]
    assert portal_state.login_attempts == 1  # მეორე ფაზა შენახული სესიით - SMS აღარ დასჭირდა
    assert next(iter(portal_state.declarations.values())).status == "წარდგენილი"

    runs = rows(pg_dsn, "SELECT kind::text, status::text, evidence_prefix IS NOT NULL FROM automation_runs"
                        " WHERE tenant_id = %s ORDER BY created_at", tenant)
    assert runs == [("prepare_zero_declaration", "succeeded", True), ("submit_declaration", "succeeded", True)]
    audit = (await world.api.get("/v1/audit/verify", headers=world.headers("owner"))).json()
    assert audit["ok"]
    actions = [r[0] for r in rows(pg_dsn, "SELECT action FROM audit_log WHERE tenant_id = %s ORDER BY seq", tenant)]
    assert {"rsge.otp_answered", "declaration.draft_prepared", "declaration.approved",
            "declaration.submitted"} <= set(actions)

    # წარდგენილზე ხელახალი მცდელობა (queue duplicate) - ნაკადი არც კი იწყება
    with pytest.raises(FlowError, match="submitted"):
        await submit_declaration(world.flow, tenant, uuid.UUID(decl))
    assert (await world.api.post(f"/v1/declarations/{decl}/submit",
                                 headers=world.headers("owner"))).status_code == 409


# ------------------------------------------------------------------ failures


@pytest.mark.browser
@pytest.mark.redis
@pytest.mark.parametrize("portal_state", [FakePortalState(otp_code=None, prefill={"line_3": "1250.00"})])
async def test_prefilled_portal_form_sends_declaration_to_review(world, pg_dsn):
    decl = await world.new_declaration(await world.setup_taxpayer())
    assert await prepare_declaration(world.flow, world.people["tenant"], uuid.UUID(decl)) == "needs_review"
    d = await world.declaration(decl)
    assert d["last_error_code"] == "prefilled_nonzero_data" and d["form_values"] == {"line_3": "1250.00"}
    assert rows(pg_dsn, "SELECT status::text, error_code FROM automation_runs WHERE declaration_id = %s", decl) == [
        ("failed", "prefilled_nonzero_data")]


@pytest.mark.browser
@pytest.mark.redis
async def test_wrong_password_disables_credential_without_retrying(world, portal_state):
    tenant = world.people["tenant"]
    decl = await world.new_declaration(await world.setup_taxpayer(password="wrong"))
    assert await prepare_declaration(world.flow, tenant, uuid.UUID(decl)) == "failed"
    d = await world.declaration(decl)
    assert d["status"] == "failed" and d["last_error_code"] == "invalid_credentials"

    # ხელახალი მცდელობა პორტალამდეც არ მიდის - ანგარიშის დაბლოკვის რისკი
    retry = await world.api.post(f"/v1/declarations/{decl}/prepare", headers=world.headers("accountant"))
    assert retry.status_code == 409 and retry.json()["detail"]["code"] == "rsge_credential_unavailable"
    with pytest.raises(FlowError, match="invalid"):
        await prepare_declaration(world.flow, tenant, uuid.UUID(decl))
    assert portal_state.login_attempts == 1


@pytest.mark.redis
async def test_automation_requires_owner_mandate(world):
    decl = await world.new_declaration(await world.setup_taxpayer(mandate=False))
    r = await world.api.post(f"/v1/declarations/{decl}/prepare", headers=world.headers("accountant"))
    assert r.status_code == 409 and r.json()["detail"]["code"] == "mandate_required"
    with pytest.raises(FlowError, match="mandate"):
        await prepare_declaration(world.flow, world.people["tenant"], uuid.UUID(decl))
    assert world.jobs.jobs == {}


@pytest.mark.redis
async def test_one_session_per_taxpayer_and_stale_runs_are_reclaimed(world, pg_dsn):
    tenant = world.people["tenant"]
    tp = await world.setup_taxpayer()
    decl = await world.new_declaration(tp)
    with psycopg.connect(pg_dsn, autocommit=True) as c:
        run = c.execute("INSERT INTO automation_runs (tenant_id, taxpayer_id, kind, status, started_at)"
                        " VALUES (%s, %s, 'canary', 'running', now()) RETURNING id", (tenant, tp)).fetchone()[0]
    with pytest.raises(FlowError, match="another rs.ge session"):
        await prepare_declaration(world.flow, tenant, uuid.UUID(decl))

    with psycopg.connect(pg_dsn, autocommit=True) as c:  # worker-ი "მოკვდა" 2 საათის წინ
        c.execute("UPDATE automation_runs SET started_at = now() - interval '2 hours' WHERE id = %s", (run,))
    world.flow.settings.rsge_base_url = "http://127.0.0.1:9"  # სწრაფი ჩავარდნა - მხოლოდ lock-ს ვამოწმებთ
    world.flow.browser_timeout_ms = 1_000
    assert await prepare_declaration(world.flow, tenant, uuid.UUID(decl)) == "failed"
    assert rows(pg_dsn, "SELECT status::text, error_code FROM automation_runs WHERE id = %s", run) == [
        ("failed", "stale")]
    # rs.ge მიუწვდომელია -> დომენის შეცდომა (არა worker-ის "ჩავარდნა"), ხელახლა ცდა შესაძლებელია
    assert (await world.declaration(decl))["last_error_code"] == "portal_unavailable"


# ------------------------------------------------------------------ planning


async def test_planner_creates_upcoming_declarations_idempotently(db, people, pg_dsn):
    tenant = people["tenant"]
    with psycopg.connect(pg_dsn, autocommit=True) as c:
        tp = c.execute("INSERT INTO taxpayers (tenant_id, tin, legal_name, legal_form, regimes) VALUES"
                       " (%s, '205000999', 'შპს გეგმა', 'llc', '{vat_payer,employer,property_tax_org}') RETURNING id",
                       (tenant,)).fetchone()[0]
    today = date(2026, 9, 28)
    assert await plan_all_tenants(db, today=today) >= 2
    planned = rows(pg_dsn, "SELECT type::text, period_start, due_date, status::text FROM declarations"
                           " WHERE taxpayer_id = %s", tp)
    # ქონების გადასახადი წლიურია და დეკლარაციის ავტომატიზაცია არ აქვს - მხოლოდ კალენდარში ჩანს
    assert set(planned) == {("vat", date(2026, 9, 1), date(2026, 10, 15), "planned"),
                            ("income_tax_withholding", date(2026, 9, 1), date(2026, 10, 15), "planned")}
    assert await plan_all_tenants(db, today=today) == 0  # იდემპოტენტური

    with psycopg.connect(pg_dsn, autocommit=True) as c:  # მომხმარებელმა გააუქმა - ხელახლა აღარ იქმნება
        c.execute("UPDATE declarations SET status = 'cancelled' WHERE taxpayer_id = %s AND type = 'vat'", (tp,))
    assert await plan_all_tenants(db, today=today) == 0


# ------------------------------------------------------------------ recovery / races


async def _approved_declaration(world, pg_dsn) -> str:
    """rs.ge-ს გარეშე: დეკლარაცია "მომზადებული" და დადასტურებული (worker-ის მდგომარეობის იმიტაცია)."""
    decl = await world.new_declaration(await world.setup_taxpayer())
    fp = "ab" * 32
    with psycopg.connect(pg_dsn, autocommit=True) as c:
        c.execute("UPDATE declarations SET status = 'awaiting_approval', form_fingerprint = %s,"
                  " form_values = '{\"line_1\": \"0\"}' WHERE id = %s", (fp, decl))
    r = await world.api.post(f"/v1/declarations/{decl}/approve", headers=world.headers("owner"),
                             json={"fingerprint": fp})
    assert r.status_code == 200, r.text
    return decl


@pytest.mark.redis
async def test_submit_endpoint_recovers_a_declaration_stuck_in_submitting(world, pg_dsn):
    decl = await _approved_declaration(world, pg_dsn)
    with psycopg.connect(pg_dsn, autocommit=True) as c:  # worker-მა "submitting" დააყენა და მოკვდა
        c.execute("UPDATE declarations SET status = 'submitting' WHERE id = %s", (decl,))
    r = await world.api.post(f"/v1/declarations/{decl}/submit", headers=world.headers("accountant"))
    assert r.status_code == 202 and r.json()["enqueued"]


@pytest.mark.redis
async def test_submit_endpoint_refuses_an_approval_of_an_older_form(world, pg_dsn):
    decl = await _approved_declaration(world, pg_dsn)
    with psycopg.connect(pg_dsn, autocommit=True) as c:  # ჩავარდა, შემდეგ ფორმა ხელახლა მომზადდა
        c.execute("UPDATE declarations SET status = 'submitting' WHERE id = %s", (decl,))
        c.execute("UPDATE declarations SET status = 'failed' WHERE id = %s", (decl,))
        c.execute("UPDATE declarations SET form_fingerprint = %s WHERE id = %s", ("cd" * 32, decl))
    r = await world.api.post(f"/v1/declarations/{decl}/submit", headers=world.headers("accountant"))
    assert r.status_code == 409 and r.json()["detail"]["code"] == "not_submittable"
    with pytest.raises(FlowError, match="without a valid approval"):
        await submit_declaration(world.flow, world.people["tenant"], uuid.UUID(decl))


@pytest.mark.redis
async def test_declaration_cancelled_while_submit_starts_is_not_sent(world, pg_dsn, monkeypatch):
    import app.workflows.declaration_flow as flow_module

    decl = await _approved_declaration(world, pg_dsn)
    real_load = flow_module.load_portal_credential

    async def load_then_user_cancels(*args, **kwargs):
        loaded = await real_load(*args, **kwargs)
        with psycopg.connect(pg_dsn, autocommit=True) as c:
            c.execute("UPDATE declarations SET status = 'cancelled' WHERE id = %s", (decl,))
        return loaded

    monkeypatch.setattr(flow_module, "load_portal_credential", load_then_user_cancels)
    with pytest.raises(FlowError, match="changed"):
        await submit_declaration(world.flow, world.people["tenant"], uuid.UUID(decl))
    assert rows(pg_dsn, "SELECT status::text FROM automation_runs WHERE declaration_id = %s", decl) == [
        ("cancelled",)]  # გაშვება დახურულია - გადამხდელი არ რჩება "დაკავებული"
    assert (await world.declaration(decl))["status"] == "cancelled"
