"""დეკლარაციის ნაკადი worker-ში: DB <-> eservice.rs.ge (Playwright) <-> ადამიანი (SMS კოდი, დადასტურება).

    prepare_declaration:  planned/... -> [login, SMS?] -> ფორმა ნულოვანია? -> დრაფტი -> awaiting_approval
    (მომხმარებელი API-ით ადასტურებს კონკრეტულ fingerprint-ს -> approved)
    submit_declaration:   approved -> submitting -> [ფორმა იგივეა?] -> გაგზავნა -> submitted (+ ქვითარი)

ყოველი გაშვება = automation_runs ჩანაწერი (ერთ გადამხდელზე ერთდროულად ერთი - DB-ის unique index),
screenshot-ები + trace.zip evidence დირექტორიაში. SMS-ის მოთხოვნა ჩანს otp_challenges-ში, რათა UI-მ
მომხმარებელს კოდი სთხოვოს (`GET /v1/rsge/otp/pending`). არასწორ პაროლზე მონაცემები `invalid` ხდება
და ავტომატიკა მას აღარ იყენებს (ანგარიშის დაბლოკვის რისკი), სანამ owner ახალ პაროლს არ შეიყვანს.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from playwright.async_api import Error as PlaywrightError
from psycopg.types.json import Jsonb

from ..accounting.models import DeclarationType
from ..accounting.tax_calendar import TaxPeriod
from ..config import Settings
from ..db.database import Database, audit
from ..rsge.browser import (
    DeclarationTarget,
    RsGeBrowserSession,
    RsGePortal,
    SubmissionApproval,
    prepare_zero_declaration,
    submit_zero_declaration,
)
from ..rsge.errors import (
    AlreadySubmittedError,
    FingerprintMismatchError,
    InvalidCredentialsError,
    OtpTimeoutError,
    PortalUnavailableError,
    PrefilledDataError,
    RsGeError,
)
from ..rsge.otp import Notifier, OtpChallenge, OtpProvider
from ..security.crypto import SecretBox
from ..services.consents import ConsentKind, has_active_consent
from ..services.taxpayers import (
    LoadedRsgeCredential,
    RsgeCredentialUnavailableError,
    load_portal_credential,
    mark_credential_problem,
    save_session_state,
)

log = logging.getLogger(__name__)

PREPARABLE = ("planned", "collecting_data", "needs_review", "draft_prepared", "awaiting_approval", "failed")
SUBMITTABLE = ("approved", "submitting", "failed")
# ეს შეცდომები ადამიანის გადაწყვეტილებას მოითხოვს (არა უბრალოდ ხელახლა ცდას)
_NEEDS_REVIEW = (PrefilledDataError, AlreadySubmittedError, FingerprintMismatchError)


class FlowError(Exception):
    """ნაკადი ვერ დაიწყო (არასწორი სტატუსი, თანხმობა/მონაცემები აკლია, გადამხდელი დაკავებულია)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class FlowContext:
    db: Database
    box: SecretBox
    settings: Settings
    # notifier -> OTP broker (prod: RedisOtpBroker). notifier იწერს challenge-ს DB-ში და აგზავნის push-ს.
    otp_factory: Callable[[Notifier], OtpProvider]
    push: Callable[[OtpChallenge], Awaitable[None]] | None = None
    browser_timeout_ms: int = 30_000
    stale_run_seconds: int = 1800  # crash-ის შემდეგ "გაჭედილი" running ჩანაწერი ამდენ ხანში თავისუფლდება


# ------------------------------------------------------------------ helpers


async def _load(ctx: FlowContext, tenant_id: UUID, declaration_id: UUID) -> dict[str, Any]:
    async with ctx.db.tenant_tx(tenant_id, actor_type="worker") as conn:
        row = await (await conn.execute(
            "SELECT id, taxpayer_id, type::text AS type, period_start, status::text AS status, form_fingerprint,"
            " approved_by, approved_at, approved_fingerprint FROM declarations WHERE id = %s",
            (declaration_id,))).fetchone()
        mandate = await has_active_consent(conn, ConsentKind.RSGE_AUTOMATION_MANDATE)
    if row is None:
        raise FlowError("not_found", f"declaration {declaration_id} not found")
    if not mandate:
        # rs.ge-ზე მოქმედება კომპანიის სახელით - მხოლოდ owner-ის მიერ გაცემული მანდატით
        raise FlowError("mandate_required", "rs.ge automation mandate (consent) is not active")
    return row


def _target(row: dict[str, Any]) -> DeclarationTarget:
    start: date = row["period_start"]
    return DeclarationTarget(str(row["id"]), DeclarationType(row["type"]), TaxPeriod(start.year, start.month))


async def _start_run(ctx: FlowContext, tenant_id: UUID, row: dict[str, Any], kind: str) -> UUID:
    async with ctx.db.tenant_tx(tenant_id, actor_type="worker") as conn:
        await conn.execute(
            "UPDATE automation_runs SET status = 'failed', error_code = 'stale', finished_at = now()"
            " WHERE taxpayer_id = %s AND status IN ('running', 'waiting_for_user')"
            " AND started_at < now() - make_interval(secs => %s)", (row["taxpayer_id"], ctx.stale_run_seconds))
        try:
            async with conn.transaction():
                run = await (await conn.execute(
                    "INSERT INTO automation_runs (tenant_id, taxpayer_id, kind, declaration_id, status, started_at)"
                    " VALUES (%s, %s, %s, %s, 'running', now()) RETURNING id",
                    (tenant_id, row["taxpayer_id"], kind, row["id"]))).fetchone()
        except psycopg.errors.UniqueViolation:
            raise FlowError("taxpayer_busy", "another rs.ge session is running for this taxpayer") from None
    return run["id"]


async def _finish_run(ctx: FlowContext, tenant_id: UUID, run_id: UUID, status: str, *, evidence: str,
                      error: Exception | None = None) -> None:
    async with ctx.db.tenant_tx(tenant_id, actor_type="worker") as conn:
        await conn.execute(
            "UPDATE automation_runs SET status = %s, finished_at = now(), error_code = %s, error_message = %s,"
            " evidence_prefix = %s WHERE id = %s",
            (status, _error_code(error) if error else None,
             str(error)[:1000] if error else None, evidence, run_id))


def _as_rsge_error(exc: RsGeError | PlaywrightError) -> RsGeError:
    """Playwright-ის ქსელური/ნავიგაციის შეცდომა = პორტალი მიუწვდომელია (ჩვეულებრივი ოპერაციული მდგომარეობა)."""
    if isinstance(exc, RsGeError):
        return exc
    return PortalUnavailableError(str(exc).splitlines()[0][:300])


def _error_code(exc: Exception) -> str:
    return exc.code if isinstance(exc, (RsGeError, FlowError)) else "internal_error"


def _evidence_dir(ctx: FlowContext, tenant_id: UUID, declaration_id: UUID, run_id: UUID) -> tuple[Path, str]:
    prefix = f"{tenant_id}/{declaration_id}/{run_id}"
    return Path(ctx.settings.evidence_dir) / prefix, prefix


def _evidence_key(prefix: str, path: Path | None) -> str | None:
    return f"{prefix}/{path.name}" if path else None


class _RunOtp:
    """OTP provider ერთი გაშვებისთვის: challenge იწერება DB-ში (UI-სთვის), ვადის გასვლა - expired."""

    def __init__(self, ctx: FlowContext, tenant_id: UUID, run_id: UUID):
        self._ctx, self._tenant_id, self._run_id = ctx, tenant_id, run_id
        self._inner = ctx.otp_factory(self._on_challenge)

    async def _on_challenge(self, challenge: OtpChallenge) -> None:
        # broker-მა challenge უკვე დაარეგისტრირა (Redis) -> API-ს შეუძლია კოდის მიღება; ახლა UI-ს ვაჩვენებთ
        async with self._ctx.db.tenant_tx(self._tenant_id, actor_type="worker") as conn:
            await conn.execute(
                "INSERT INTO otp_challenges (id, tenant_id, automation_run_id, phone_hint, expires_at)"
                " VALUES (%s, %s, %s, %s, %s)",
                (challenge.id, self._tenant_id, self._run_id, challenge.phone_hint, challenge.expires_at))
            await conn.execute("UPDATE automation_runs SET status = 'waiting_for_user' WHERE id = %s", (self._run_id,))
        if self._ctx.push is not None:
            await self._ctx.push(challenge)

    async def request_code(self, challenge: OtpChallenge) -> str:
        try:
            return await self._inner.request_code(challenge)
        except OtpTimeoutError:
            await self._set_challenge(challenge.id, "expired")
            raise
        finally:
            async with self._ctx.db.tenant_tx(self._tenant_id, actor_type="worker") as conn:
                await conn.execute("UPDATE automation_runs SET status = 'running' WHERE id = %s"
                                   " AND status = 'waiting_for_user'", (self._run_id,))

    async def _set_challenge(self, challenge_id: str, status: str) -> None:
        async with self._ctx.db.tenant_tx(self._tenant_id, actor_type="worker") as conn:
            await conn.execute("UPDATE otp_challenges SET status = %s WHERE id = %s AND status = 'pending'",
                               (status, challenge_id))


def _session(ctx: FlowContext, evidence: Path, cred: LoadedRsgeCredential) -> RsGeBrowserSession:
    return RsGeBrowserSession(evidence_dir=evidence, storage_state=cred.storage_state,
                              headless=ctx.settings.rsge_headless,
                              executable_path=ctx.settings.rsge_chromium_executable,
                              default_timeout_ms=ctx.browser_timeout_ms)


async def _fail(ctx: FlowContext, tenant_id: UUID, row: dict[str, Any], run_id: UUID, prefix: str,
                cred: LoadedRsgeCredential, exc: Exception, *, from_statuses: tuple[str, ...]) -> str:
    """rs.ge შეცდომა -> დეკლარაციის სტატუსი (needs_review ან failed), გაშვება -> failed."""
    code = _error_code(exc)
    status = "needs_review" if isinstance(exc, _NEEDS_REVIEW) else "failed"
    if isinstance(exc, InvalidCredentialsError):
        await mark_credential_problem(ctx.db, tenant_id=tenant_id, credential_id=cred.id, error_code=code,
                                      status="invalid")
    form_values = exc.fields if isinstance(exc, PrefilledDataError) else None
    async with ctx.db.tenant_tx(tenant_id, actor_type="worker") as conn:
        await conn.execute(
            "UPDATE declarations SET status = %s, last_error_code = %s,"
            " form_values = coalesce(%s, form_values) WHERE id = %s AND status::text = ANY(%s)",
            (status, code, Jsonb(form_values) if form_values is not None else None, row["id"], list(from_statuses)))
        await audit(conn, tenant_id=tenant_id, actor_type="worker", actor_id=None, action="rsge.run_failed",
                    entity_type="declaration", entity_id=str(row["id"]),
                    details={"run_id": str(run_id), "error_code": code, "declaration_status": status,
                             "screenshot": _evidence_key(prefix, getattr(exc, "screenshot", None))})
    await _finish_run(ctx, tenant_id, run_id, "failed", evidence=prefix, error=exc)
    log.warning("rs.ge run %s failed: %s", run_id, code)
    return status


# ------------------------------------------------------------------ use cases


async def prepare_declaration(ctx: FlowContext, tenant_id: UUID, declaration_id: UUID) -> str:
    """აბრუნებს დეკლარაციის ახალ სტატუსს: awaiting_approval | needs_review | failed."""
    row = await _load(ctx, tenant_id, declaration_id)
    if row["status"] not in PREPARABLE:
        raise FlowError("not_preparable", f"declaration is {row['status']}")
    try:
        cred = await load_portal_credential(ctx.db, ctx.box, tenant_id, row["taxpayer_id"])
    except RsgeCredentialUnavailableError as exc:
        raise FlowError("rsge_credential_unavailable", str(exc)) from None
    run_id = await _start_run(ctx, tenant_id, row, "prepare_zero_declaration")
    evidence, prefix = _evidence_dir(ctx, tenant_id, declaration_id, run_id)
    target = _target(row)
    try:
        async with _session(ctx, evidence, cred) as session:
            portal = RsGePortal(session, base_url=ctx.settings.rsge_base_url)
            await portal.ensure_logged_in(cred.credentials, _RunOtp(ctx, tenant_id, run_id),
                                          otp_ttl_seconds=ctx.settings.rsge_otp_timeout_seconds)
            prepared = await prepare_zero_declaration(portal, target)
    except (RsGeError, PlaywrightError) as exc:
        return await _fail(ctx, tenant_id, row, run_id, prefix, cred, _as_rsge_error(exc), from_statuses=PREPARABLE)
    except Exception as exc:
        await _fail(ctx, tenant_id, row, run_id, prefix, cred, exc, from_statuses=PREPARABLE)
        raise

    if session.final_storage_state:
        await save_session_state(ctx.db, ctx.box, tenant_id=tenant_id, credential_id=cred.id,
                                 storage_state=session.final_storage_state)
    async with ctx.db.tenant_tx(tenant_id, actor_type="worker") as conn:
        cur = await conn.execute(
            "UPDATE declarations SET status = 'draft_prepared', is_zero = true, form_values = %s,"
            " form_fingerprint = %s, draft_screenshot_key = %s, last_error_code = NULL"
            " WHERE id = %s AND status::text = ANY(%s)",
            (Jsonb(prepared.form_values), prepared.fingerprint, _evidence_key(prefix, prepared.screenshot),
             declaration_id, list(PREPARABLE)))
        if cur.rowcount == 0:  # ამასობაში სხვამ შეცვალა (მაგ. გააუქმა) - შედეგს არ ვწერთ
            status = "declaration_changed"
        else:
            await conn.execute("UPDATE declarations SET status = 'awaiting_approval' WHERE id = %s", (declaration_id,))
            await audit(conn, tenant_id=tenant_id, actor_type="worker", actor_id=None,
                        action="declaration.draft_prepared", entity_type="declaration", entity_id=str(declaration_id),
                        details={"run_id": str(run_id), "fingerprint": prepared.fingerprint})
            status = "awaiting_approval"
    await _finish_run(ctx, tenant_id, run_id, "succeeded" if status != "declaration_changed" else "cancelled",
                      evidence=prefix)
    return status


async def submit_declaration(ctx: FlowContext, tenant_id: UUID, declaration_id: UUID) -> str:
    """აბრუნებს დეკლარაციის ახალ სტატუსს: submitted | needs_review | failed."""
    row = await _load(ctx, tenant_id, declaration_id)
    approval_valid = (row["approved_by"] is not None and row["approved_fingerprint"] is not None
                      and row["approved_fingerprint"] == row["form_fingerprint"])
    if row["status"] not in SUBMITTABLE or not approval_valid:
        raise FlowError("not_submittable", f"declaration is {row['status']} without a valid approval")
    try:
        cred = await load_portal_credential(ctx.db, ctx.box, tenant_id, row["taxpayer_id"])
    except RsgeCredentialUnavailableError as exc:
        raise FlowError("rsge_credential_unavailable", str(exc)) from None
    run_id = await _start_run(ctx, tenant_id, row, "submit_declaration")
    evidence, prefix = _evidence_dir(ctx, tenant_id, declaration_id, run_id)
    async with ctx.db.tenant_tx(tenant_id, actor_type="worker") as conn:
        # "submitting" = rs.ge-ზე შესაძლოა უკვე გაიგზავნა: crash-ის შემდეგ ჯერ is_submitted მოწმდება
        cur = await conn.execute(
            "UPDATE declarations SET status = 'submitting' WHERE id = %s AND status::text = ANY(%s)"
            " AND approved_fingerprint = form_fingerprint RETURNING id", (declaration_id, list(SUBMITTABLE)))
        changed = await cur.fetchone() is None
    if changed:  # ამასობაში გააუქმეს/ხელახლა მოამზადეს - გაშვებას ვხურავთ, არაფერს ვაგზავნით
        await _finish_run(ctx, tenant_id, run_id, "cancelled", evidence=prefix)
        raise FlowError("declaration_changed", "declaration changed before submission started")
    approval = SubmissionApproval(declaration_id=str(declaration_id), fingerprint=row["approved_fingerprint"],
                                  approved_by_user_id=str(row["approved_by"]), approved_at=row["approved_at"])
    try:
        async with _session(ctx, evidence, cred) as session:
            portal = RsGePortal(session, base_url=ctx.settings.rsge_base_url)
            await portal.ensure_logged_in(cred.credentials, _RunOtp(ctx, tenant_id, run_id),
                                          otp_ttl_seconds=ctx.settings.rsge_otp_timeout_seconds)
            receipt = await submit_zero_declaration(portal, _target(row), approval)
    except (RsGeError, PlaywrightError) as exc:
        return await _fail(ctx, tenant_id, row, run_id, prefix, cred, _as_rsge_error(exc),
                           from_statuses=("submitting",))
    except Exception as exc:
        await _fail(ctx, tenant_id, row, run_id, prefix, cred, exc, from_statuses=("submitting",))
        raise

    if session.final_storage_state:
        await save_session_state(ctx.db, ctx.box, tenant_id=tenant_id, credential_id=cred.id,
                                 storage_state=session.final_storage_state)
    async with ctx.db.tenant_tx(tenant_id, actor_type="worker") as conn:
        await conn.execute(
            "UPDATE declarations SET status = 'submitted', rs_receipt_number = %s, submitted_at = %s,"
            " receipt_screenshot_key = %s, last_error_code = NULL WHERE id = %s",
            (receipt.receipt_number, receipt.submitted_at, _evidence_key(prefix, receipt.screenshot), declaration_id))
        await audit(conn, tenant_id=tenant_id, actor_type="worker", actor_id=None, action="declaration.submitted",
                    entity_type="declaration", entity_id=str(declaration_id),
                    details={"run_id": str(run_id), "receipt_number": receipt.receipt_number,
                             "approved_by": str(row["approved_by"]), "fingerprint": row["approved_fingerprint"]})
    await _finish_run(ctx, tenant_id, run_id, "succeeded", evidence=prefix)
    return "submitted"
