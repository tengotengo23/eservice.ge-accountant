"""rs.ge Playwright ნაკადი ყალბ პორტალზე (ნამდვილი Chromium, localhost).

ეს ამოწმებს ჩვენი კოდის ლოგიკას (2FA, CAPTCHA, prefilled, idempotency, fingerprint),
და არა eservice.rs.ge-ს რეალურ DOM-ს - იხ. app/rsge/selectors.py.
"""

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.accounting.models import DeclarationType
from app.accounting.tax_calendar import TaxPeriod
from app.rsge.browser import (
    DeclarationTarget,
    RsGeBrowserSession,
    RsGeCredentials,
    RsGePortal,
    SubmissionApproval,
    prepare_zero_declaration,
    submit_zero_declaration,
)
from app.rsge.errors import (
    AlreadySubmittedError,
    FingerprintMismatchError,
    HumanInterventionRequired,
    InvalidCredentialsError,
    OtpRejectedError,
    PrefilledDataError,
)
from app.rsge.otp import InMemoryOtpBroker
from tests.fake_rsge.server import FakePortal, FakePortalState

pytestmark = pytest.mark.browser

CHROMIUM = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or (
    "/opt/pw-browsers/chromium" if Path("/opt/pw-browsers/chromium").exists() else None
)
CREDS = RsGeCredentials(tenant_id="t-1", credential_id="c-1", username="405123456", password="correct-horse")
TARGET = DeclarationTarget("decl-1", DeclarationType.VAT, TaxPeriod(2026, 9))


class UserAtPhone:
    """იმიტირებს მომხმარებელს: შეტყობინებას იღებს და SMS კოდს აგზავნის API-ით."""

    def __init__(self, code: str):
        self.code = code
        self.challenges = []
        self.broker = InMemoryOtpBroker(notifier=self.notify)

    async def notify(self, challenge):
        self.challenges.append(challenge)

        async def reply():
            await asyncio.sleep(0.05)
            assert await self.broker.submit_code(challenge.id, self.code, tenant_id=challenge.tenant_id)

        asyncio.get_running_loop().create_task(reply())


def session(tmp_path: Path, storage_state=None) -> RsGeBrowserSession:
    return RsGeBrowserSession(evidence_dir=tmp_path / "evidence", storage_state=storage_state,
                              executable_path=CHROMIUM, default_timeout_ms=5_000)


async def test_zero_vat_two_phase_flow_with_sms_code(tmp_path):
    state = FakePortalState()
    user = UserAtPhone(state.otp_code)
    with FakePortal(state) as portal_server:
        async with session(tmp_path) as s:
            portal = RsGePortal(s, base_url=portal_server.base_url)
            await portal.ensure_logged_in(CREDS, user.broker)
            prepared = await prepare_zero_declaration(portal, TARGET)
        storage_state = s.final_storage_state

        assert user.challenges[0].phone_hint == "***-**-12"
        assert set(prepared.form_values.values()) == {"0"}
        draft = next(iter(state.declarations.values()))
        assert draft.status == "დრაფტი" and draft.period == "2026-09"
        assert (tmp_path / "evidence" / "trace.zip").exists()
        assert prepared.screenshot.exists()

        # --- მომხმარებელმა ნახა screenshot და დაადასტურა; იგივე სესია (cookies) - SMS აღარ სჭირდება
        approval = SubmissionApproval("decl-1", prepared.fingerprint, "user-7", datetime.now(timezone.utc))
        async with session(tmp_path / "submit", storage_state) as s2:
            portal = RsGePortal(s2, base_url=portal_server.base_url)
            await portal.ensure_logged_in(CREDS, InMemoryOtpBroker())
            receipt = await submit_zero_declaration(portal, TARGET, approval)

        assert receipt.receipt_number == f"DKL-2026-{draft.id:06d}"
        assert draft.status == "წარდგენილი"
        assert state.login_attempts == 1  # მეორე ფაზაში სესია ხელახლა გამოიყენა

        # --- განმეორებითი გაშვება (retry/queue duplicate) ორჯერ არ წარადგენს
        async with session(tmp_path / "again", storage_state) as s3:
            portal = RsGePortal(s3, base_url=portal_server.base_url)
            with pytest.raises(AlreadySubmittedError):
                await submit_zero_declaration(portal, TARGET, approval)


async def test_prefilled_form_aborts_zero_declaration(tmp_path):
    state = FakePortalState(otp_code=None, prefill={"line_3": "1250.00"})
    with FakePortal(state) as srv:
        async with session(tmp_path) as s:
            portal = RsGePortal(s, base_url=srv.base_url)
            await portal.ensure_logged_in(CREDS, InMemoryOtpBroker())
            with pytest.raises(PrefilledDataError) as exc:
                await prepare_zero_declaration(portal, TARGET)
    assert exc.value.fields == {"line_3": "1250.00"}
    assert exc.value.screenshot.exists()


async def test_captcha_stops_automation(tmp_path):
    with FakePortal(FakePortalState(captcha=True)) as srv:
        async with session(tmp_path) as s:
            portal = RsGePortal(s, base_url=srv.base_url)
            with pytest.raises(HumanInterventionRequired) as exc:
                await portal.ensure_logged_in(CREDS, InMemoryOtpBroker())
    assert exc.value.reason == "captcha"


async def test_wrong_password_is_not_retried(tmp_path):
    state = FakePortalState()
    bad = RsGeCredentials("t-1", "c-1", "405123456", "wrong")
    with FakePortal(state) as srv:
        async with session(tmp_path) as s:
            with pytest.raises(InvalidCredentialsError):
                await RsGePortal(s, base_url=srv.base_url).ensure_logged_in(bad, InMemoryOtpBroker())
    assert state.login_attempts == 1


async def test_wrong_sms_code_is_not_retried(tmp_path):
    state = FakePortalState()
    user = UserAtPhone("000000")
    with FakePortal(state) as srv:
        async with session(tmp_path) as s:
            with pytest.raises(OtpRejectedError):
                await RsGePortal(s, base_url=srv.base_url).ensure_logged_in(CREDS, user.broker)
    assert state.otp_attempts == 1


async def test_form_changed_after_approval_blocks_submission(tmp_path):
    state = FakePortalState(otp_code=None)
    with FakePortal(state) as srv:
        async with session(tmp_path) as s:
            portal = RsGePortal(s, base_url=srv.base_url)
            await portal.ensure_logged_in(CREDS, InMemoryOtpBroker())
            prepared = await prepare_zero_declaration(portal, TARGET)
            # დადასტურებასა და გაგზავნას შორის rs.ge-ზე ახალი ანგარიშ-ფაქტურა აისახა
            next(iter(state.declarations.values())).values["line_2"] = "90.00"
            approval = SubmissionApproval("decl-1", prepared.fingerprint, "user-7", datetime.now(timezone.utc))
            with pytest.raises(FingerprintMismatchError):
                await submit_zero_declaration(portal, TARGET, approval)
    assert next(iter(state.declarations.values())).status == "დრაფტი"
