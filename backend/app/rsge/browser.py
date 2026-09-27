"""eservice.rs.ge ავტომატიზაცია (Playwright): ავტორიზაცია, 2FA, ნულოვანი დეკლარაცია.

ნულოვანი დეკლარაცია ორ ფაზად:

  1. prepare_zero_declaration  - შესვლა -> ფორმის გახსნა -> ყველა ველი ნულია? -> დრაფტის შენახვა ->
                                 screenshot + ფორმის "ანაბეჭდი" (fingerprint). ᲐᲠᲐᲤᲔᲠᲡ ᲒᲖᲐᲕᲜᲘᲡ.
  2. submit_zero_declaration   - მხოლოდ მომხმარებლის დადასტურებით (SubmissionApproval, რომელიც
                                 fingerprint-ს შეიცავს) -> ფორმა ხელახლა მოწმდება -> გაგზავნა ->
                                 ქვითრის ნომერი + screenshot.

უსაფრთხოების წესები:
* CAPTCHA-ს ავტომატურად არ ვხსნით და 2FA-ს გვერდს არ ვუვლით -> HumanInterventionRequired / OTP broker.
* არასწორ პაროლზე/SMS კოდზე ხელახლა არ ვცდით (ანგარიშის დაბლოკვის რისკი).
* ერთ rs.ge ანგარიშზე ერთდროულად მხოლოდ ერთი სესია (worker-ის დონეზე lock, იხ. docs).
* ყოველი ნაბიჯი ფიქსირდება: screenshot-ები + Playwright trace (აუდიტი და დავის შემთხვევაში მტკიცებულება).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from ..accounting.models import DeclarationType
from ..accounting.tax_calendar import TaxPeriod
from .errors import (
    AlreadySubmittedError,
    ApprovalError,
    FingerprintMismatchError,
    HumanInterventionRequired,
    InvalidCredentialsError,
    OtpRejectedError,
    PortalLayoutChangedError,
    PrefilledDataError,
    RsGeError,
)
from .otp import OtpChallenge, OtpProvider
from .selectors import DEFAULT_SELECTORS, RsGeSelectors

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RsGeCredentials:
    tenant_id: str
    credential_id: str
    username: str
    password: str = field(repr=False)


@dataclass(frozen=True)
class DeclarationTarget:
    declaration_id: str  # ჩვენი DB-ის declarations.id
    declaration_type: DeclarationType
    period: TaxPeriod


@dataclass(frozen=True)
class PreparedDeclaration:
    target: DeclarationTarget
    form_values: dict[str, str]
    fingerprint: str
    screenshot: Path
    prepared_at: datetime


@dataclass(frozen=True)
class SubmissionApproval:
    """მომხმარებლის ცალსახა თანხმობა კონკრეტულ, ნანახ ფორმაზე (fingerprint)."""

    declaration_id: str
    fingerprint: str
    approved_by_user_id: str
    approved_at: datetime


@dataclass(frozen=True)
class SubmissionReceipt:
    receipt_number: str
    submitted_at: datetime
    screenshot: Path


def form_fingerprint(target: DeclarationTarget, values: dict[str, str]) -> str:
    payload = json.dumps(
        {"type": target.declaration_type.value, "period": str(target.period), "values": sorted(values.items())},
        ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def parse_amount(raw: str) -> Decimal:
    cleaned = raw.replace(" ", "").replace(" ", "").replace(",", ".").strip()
    if cleaned in ("", "-"):
        return Decimal("0")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        raise PortalLayoutChangedError(f"unexpected amount format: {raw!r}") from None


class EvidenceRecorder:
    """screenshot-ები + manifest (sha256) - ინახება S3-ში (Object Lock / WORM) აუდიტისთვის."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.items: list[dict[str, str]] = []

    async def screenshot(self, page: Page, label: str) -> Path:
        path = self.directory / f"{len(self.items):02d}-{label}.png"
        data = await page.screenshot(path=str(path), full_page=True)
        self.items.append({"label": label, "file": path.name, "sha256": hashlib.sha256(data).hexdigest(),
                           "url": page.url, "at": datetime.now(UTC).isoformat()})
        return path

    def write_manifest(self) -> Path:
        path = self.directory / "manifest.json"
        path.write_text(json.dumps(self.items, ensure_ascii=False, indent=2))
        return path


class RsGeBrowserSession:
    """ერთი იზოლირებული ბრაუზერის კონტექსტი ერთი rs.ge ანგარიშისთვის.

    storage_state (cookies/localStorage) შენახვამდე იშიფრება SecretBox-ით (purpose="rsge_session_state"),
    რათა ყოველ ჯერზე ხელახალი SMS ავტორიზაცია არ დაგვჭირდეს, სანამ სესია ვალიდურია.
    """

    def __init__(self, *, evidence_dir: Path, storage_state: dict[str, Any] | None = None, headless: bool = True,
                 executable_path: str | None = None, default_timeout_ms: int = 30_000):
        self._evidence_dir = evidence_dir
        self._storage_state = storage_state
        self._headless = headless
        self._executable_path = executable_path
        self._timeout = default_timeout_ms
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None
        self.evidence = EvidenceRecorder(evidence_dir)
        self.final_storage_state: dict[str, Any] | None = None

    async def __aenter__(self) -> RsGeBrowserSession:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=self._headless,
                                                       executable_path=self._executable_path)
        self._context = await self._browser.new_context(
            locale="ka-GE", timezone_id="Asia/Tbilisi", storage_state=self._storage_state,
            viewport={"width": 1366, "height": 900},
        )
        await self._context.tracing.start(screenshots=True, snapshots=True)
        self.page = await self._context.new_page()
        self.page.set_default_timeout(self._timeout)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            if self._context is not None:
                if exc_type is None:
                    self.final_storage_state = await self._context.storage_state()
                await self._context.tracing.stop(path=str(self._evidence_dir / "trace.zip"))
                self.evidence.write_manifest()
        finally:
            if self._browser is not None:
                await self._browser.close()
            if self._pw is not None:
                await self._pw.stop()


class RsGePortal:
    def __init__(self, session: RsGeBrowserSession, *, base_url: str, selectors: RsGeSelectors = DEFAULT_SELECTORS):
        if session.page is None:
            raise RuntimeError("session is not started")
        self.page = session.page
        self.evidence = session.evidence
        self.base_url = base_url.rstrip("/")
        self.s = selectors

    # ------------------------------------------------------------ helpers

    def _url(self, path: str) -> str:
        return self.base_url + path

    async def _expect(self, selector: str, what: str, timeout: int | None = None) -> None:
        try:
            await self.page.locator(selector).first.wait_for(state="visible", timeout=timeout)
        except PlaywrightTimeoutError:
            shot = await self.evidence.screenshot(self.page, f"missing-{what}")
            raise PortalLayoutChangedError(f"expected element not found: {what} ({selector})",
                                           screenshot=shot) from None

    async def _visible(self, selector: str) -> bool:
        return await self.page.locator(selector).first.is_visible()

    # ------------------------------------------------------------ authentication

    async def is_logged_in(self) -> bool:
        await self.page.goto(self._url(self.s.home_path))
        return await self._visible(self.s.logged_in_marker)

    async def ensure_logged_in(self, creds: RsGeCredentials, otp: OtpProvider, *, otp_ttl_seconds: int = 180) -> None:
        if await self.is_logged_in():
            log.info("rs.ge session reused for credential %s", creds.credential_id)
            return
        await self.login(creds, otp, otp_ttl_seconds=otp_ttl_seconds)

    async def login(self, creds: RsGeCredentials, otp: OtpProvider, *, otp_ttl_seconds: int = 180) -> None:
        page, s = self.page, self.s
        await page.goto(self._url(s.login_path))
        await self._raise_if_captcha("login-page")
        await self._expect(s.username_input, "username input")
        await page.locator(s.username_input).first.fill(creds.username)
        await page.locator(s.password_input).first.fill(creds.password)
        await page.locator(s.login_button).first.click()

        outcome = await self._wait_for_any({
            "ok": s.logged_in_marker, "otp": s.otp_input, "captcha": s.captcha_marker,
            "error": s.login_error, "password_change": s.forced_password_change_marker,
        }, what="login result")
        if outcome == "otp":
            await self._complete_otp(creds, otp, otp_ttl_seconds)
        elif outcome == "captcha":
            await self._raise_if_captcha("after-login")
        elif outcome == "error":
            shot = await self.evidence.screenshot(page, "login-error")
            raise InvalidCredentialsError("rs.ge rejected the username/password", screenshot=shot)
        elif outcome == "password_change":
            shot = await self.evidence.screenshot(page, "password-change")
            raise HumanInterventionRequired("password_change", "rs.ge requires a password change", screenshot=shot)
        await self.evidence.screenshot(page, "logged-in")

    async def _complete_otp(self, creds: RsGeCredentials, otp: OtpProvider, ttl: int) -> None:
        page, s = self.page, self.s
        body = await page.locator("body").inner_text()
        hint = m.group(0) if (m := re.search(r"[*\d][*\d\- ]{5,}\d{2}", body)) else None
        challenge = OtpChallenge(tenant_id=creds.tenant_id, credential_id=creds.credential_id,
                                 phone_hint=hint, ttl_seconds=ttl)
        log.info("rs.ge requested SMS code; waiting for user (challenge %s)", challenge.id)
        code = await otp.request_code(challenge)
        await page.locator(s.otp_input).first.fill(code)
        await page.locator(s.otp_submit).first.click()
        outcome = await self._wait_for_any({"ok": s.logged_in_marker, "error": s.otp_error,
                                            "captcha": s.captcha_marker}, what="otp result")
        if outcome == "error":
            shot = await self.evidence.screenshot(page, "otp-rejected")
            raise OtpRejectedError("rs.ge rejected the SMS code", screenshot=shot)
        if outcome == "captcha":
            await self._raise_if_captcha("after-otp")

    async def _raise_if_captcha(self, where: str) -> None:
        if await self._visible(self.s.captcha_marker):
            shot = await self.evidence.screenshot(self.page, f"captcha-{where}")
            raise HumanInterventionRequired(
                "captcha", "rs.ge shows a CAPTCHA; the user must complete the login interactively", screenshot=shot)

    async def _wait_for_any(self, selectors: dict[str, str], *, what: str, timeout: int = 20_000) -> str:
        combined = None
        for sel in selectors.values():
            loc = self.page.locator(sel)
            combined = loc if combined is None else combined.or_(loc)
        try:
            await combined.first.wait_for(state="visible", timeout=timeout)
        except PlaywrightTimeoutError:
            shot = await self.evidence.screenshot(self.page, f"timeout-{what.replace(' ', '-')}")
            raise PortalLayoutChangedError(f"no known state after {what}", screenshot=shot) from None
        for name, sel in selectors.items():
            if await self._visible(sel):
                return name
        raise PortalLayoutChangedError(f"ambiguous state after {what}")

    # ------------------------------------------------------------ declarations

    def _matching_rows(self, target: DeclarationTarget, status_text: str):
        label = self.s.declaration_type_labels[target.declaration_type]
        rows = self.page.locator(self.s.declarations_rows)
        return rows.filter(has_text=label).filter(has_text=str(target.period)).filter(has_text=status_text)

    async def open_declarations(self) -> None:
        await self.page.goto(self._url(self.s.declarations_path))
        await self._expect(self.s.new_declaration_button, "new declaration button")

    async def is_submitted(self, target: DeclarationTarget) -> bool:
        await self.open_declarations()
        return await self._matching_rows(target, self.s.status_submitted_text).count() > 0

    async def open_form(self, target: DeclarationTarget) -> None:
        """არსებული დრაფტის გახსნა, ან ახლის შექმნა."""
        page, s = self.page, self.s
        await self.open_declarations()
        drafts = self._matching_rows(target, s.status_draft_text)
        if await drafts.count() > 0:
            await drafts.first.locator(s.row_open_link).click()
        else:
            await page.locator(s.new_declaration_button).first.click()
            await self._expect(s.declaration_type_select, "declaration type select")
            await page.locator(s.declaration_type_select).select_option(
                label=s.declaration_type_labels[target.declaration_type])
            await page.locator(s.period_year_select).select_option(str(target.period.year))
            if target.period.month is not None:
                await page.locator(s.period_month_select).select_option(str(target.period.month))
            await page.locator(s.continue_button).first.click()
        await self._expect(s.form_root, "declaration form")

    async def read_amounts(self) -> dict[str, str]:
        values: dict[str, str] = {}
        inputs = self.page.locator(self.s.amount_inputs)
        for i in range(await inputs.count()):
            field_ = inputs.nth(i)
            name = await field_.get_attribute("name") or await field_.get_attribute("id") or f"field_{i}"
            values[name] = await field_.input_value()
        if not values:
            raise PortalLayoutChangedError("declaration form has no amount fields")
        return values

    async def fill_empty_with_zero(self) -> None:
        inputs = self.page.locator(self.s.amount_inputs)
        for i in range(await inputs.count()):
            field_ = inputs.nth(i)
            if (await field_.input_value()).strip() == "" and await field_.is_editable():
                await field_.fill("0")

    async def save_draft(self) -> None:
        await self.page.locator(self.s.save_draft_button).first.click()
        await self._expect(self.s.draft_saved_marker, "draft saved confirmation")

    async def submit(self) -> str:
        page, s = self.page, self.s

        async def on_dialog(dialog) -> None:
            if s.submit_confirm_text in dialog.message:
                await dialog.accept()
            else:  # მოულოდნელი დიალოგი - არ ვეთანხმებით
                log.warning("unexpected dialog while submitting: %s", dialog.message)
                await dialog.dismiss()

        page.once("dialog", on_dialog)
        await page.locator(s.submit_button).first.click()
        outcome = await self._wait_for_any({"ok": s.submission_success, "error": s.submission_error},
                                           what="submission result", timeout=60_000)
        if outcome == "error":
            message = await page.locator(s.submission_error).first.inner_text()
            shot = await self.evidence.screenshot(page, "submission-error")
            raise RsGeError(f"rs.ge rejected the declaration: {message}", screenshot=shot)
        return (await page.locator(s.receipt_number).first.inner_text()).strip()


# ---------------------------------------------------------------- use cases


def _nonzero(values: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in values.items() if parse_amount(v) != 0}


async def prepare_zero_declaration(portal: RsGePortal, target: DeclarationTarget) -> PreparedDeclaration:
    if await portal.is_submitted(target):
        raise AlreadySubmittedError(f"{target.declaration_type.value} {target.period} is already submitted")
    await portal.open_form(target)
    shot = await portal.evidence.screenshot(portal.page, "form-opened")
    nonzero = _nonzero(await portal.read_amounts())
    if nonzero:
        # rs.ge ავსებს ფორმას ანგარიშ-ფაქტურებიდან/ზედნადებებიდან: ნული აქ ნიშნავს არასწორ დეკლარაციას
        raise PrefilledDataError(nonzero, screenshot=shot)
    if portal.s.fill_empty_with_zero:
        await portal.fill_empty_with_zero()
    values = await portal.read_amounts()
    await portal.save_draft()
    shot = await portal.evidence.screenshot(portal.page, "draft-saved")
    return PreparedDeclaration(target=target, form_values=values, fingerprint=form_fingerprint(target, values),
                               screenshot=shot, prepared_at=datetime.now(UTC))


async def submit_zero_declaration(portal: RsGePortal, target: DeclarationTarget,
                                  approval: SubmissionApproval) -> SubmissionReceipt:
    if approval.declaration_id != target.declaration_id:
        raise ApprovalError("approval belongs to a different declaration")
    if await portal.is_submitted(target):
        raise AlreadySubmittedError(f"{target.declaration_type.value} {target.period} is already submitted")
    await portal.open_form(target)
    values = await portal.read_amounts()
    if _nonzero(values) or form_fingerprint(target, values) != approval.fingerprint:
        shot = await portal.evidence.screenshot(portal.page, "fingerprint-mismatch")
        raise FingerprintMismatchError("form changed after the user approved it", screenshot=shot)
    await portal.evidence.screenshot(portal.page, "before-submit")
    receipt_number = await portal.submit()
    shot = await portal.evidence.screenshot(portal.page, "submitted")
    return SubmissionReceipt(receipt_number=receipt_number, submitted_at=datetime.now(UTC), screenshot=shot)
