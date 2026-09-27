"""REST API (v1). მძიმე ოპერაციები (rs.ge, დიდი დოკუმენტები) production-ში რიგში (queue) იგზავნება."""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from pydantic import BaseModel, Field, SecretStr

from ..accounting.ai_accountant import AIAccountant
from ..accounting.documents import MAX_UPLOAD_BYTES, UnsupportedDocumentError
from ..accounting.tax_calendar import Regime, upcoming_deadlines
from ..db.database import verify_audit_chain
from ..llm import LLMAuthError, LLMError, LLMRefusalError, LLMRequestError, Provider
from ..llm.router import default_model_for
from ..services.consents import ConsentKind, grant_consent, has_active_consent, list_consents, revoke_consent
from ..services.declarations import (
    DeclarationNotFoundError,
    DeclarationStateError,
    approve_declaration,
    get_declaration,
    list_declarations,
)
from ..services.llm_keys import (
    ModelNotAvailableError,
    NoLlmCredentialError,
    list_llm_keys,
    load_default_credentials,
    register_llm_key,
    revoke_llm_key,
    set_default_llm_key,
)
from ..services.llm_usage import record_llm_call, usage_summary
from .deps import Principal, client_ip, get_db, get_otp_broker, get_router, get_secret_box, require_role

router = APIRouter(prefix="/v1")

Owner = Annotated[Principal, Depends(require_role("owner"))]
Member = Annotated[Principal, Depends(require_role("owner", "accountant", "viewer"))]
Staff = Annotated[Principal, Depends(require_role("owner", "accountant"))]


def _llm_http_error(exc: LLMError, *, stored_key: bool) -> HTTPException:
    if isinstance(exc, LLMAuthError):
        message = "stored API key was rejected; update it" if stored_key else "API key rejected by the provider"
        return HTTPException(status.HTTP_400_BAD_REQUEST, message)
    if isinstance(exc, LLMRefusalError):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "the model declined; manual review needed")
    if isinstance(exc, LLMRequestError):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))
    return HTTPException(status.HTTP_502_BAD_GATEWAY, "LLM provider error, try again later")


# ------------------------------------------------------------------ session


class MeOut(BaseModel):
    tenant_id: UUID
    user_id: UUID
    role: str


@router.get("/me", response_model=MeOut, tags=["session"])
async def me(principal: Member) -> MeOut:
    return MeOut(tenant_id=principal.tenant_id, user_id=principal.user_id, role=principal.role)


# ------------------------------------------------------------------ LLM keys


class ProviderInfo(BaseModel):
    id: Provider
    default_model: str


class CreateLlmKey(BaseModel):
    provider: Provider
    api_key: SecretStr = Field(min_length=10, max_length=512)
    model: str | None = Field(default=None, max_length=100)
    make_default: bool = True


class LlmKeyOut(BaseModel):
    id: UUID
    provider: Provider
    model: str
    key_hint: str
    is_default: bool
    status: str
    created_at: datetime
    available_models: list[str] | None = None


class UsageOut(BaseModel):
    provider: str
    model: str
    purpose: str
    calls: int
    failed_calls: int
    input_tokens: int
    output_tokens: int


@router.get("/llm/providers", response_model=list[ProviderInfo], tags=["llm"])
async def providers() -> list[ProviderInfo]:
    return [ProviderInfo(id=p, default_model=default_model_for(p)) for p in Provider]


@router.post("/llm/credentials", response_model=LlmKeyOut, status_code=status.HTTP_201_CREATED, tags=["llm"])
async def create_llm_key(body: CreateLlmKey, principal: Owner, request: Request, db=Depends(get_db),
                         llm=Depends(get_router), box=Depends(get_secret_box)) -> LlmKeyOut:
    try:
        view, available = await register_llm_key(
            db, llm, box, tenant_id=principal.tenant_id, user_id=principal.user_id, provider=body.provider,
            api_key=body.api_key.get_secret_value(), model=body.model, make_default=body.make_default,
            ip=client_ip(request))
    except ModelNotAvailableError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            {"message": str(exc), "available_models": exc.available}) from None
    except LLMError as exc:
        raise _llm_http_error(exc, stored_key=False) from None
    return LlmKeyOut(**view.__dict__, available_models=available)


@router.get("/llm/credentials", response_model=list[LlmKeyOut], tags=["llm"])
async def get_llm_keys(principal: Member, db=Depends(get_db)) -> list[LlmKeyOut]:
    return [LlmKeyOut(**v.__dict__) for v in await list_llm_keys(db, principal.tenant_id)]


@router.post("/llm/credentials/{credential_id}/default", response_model=LlmKeyOut, tags=["llm"])
async def make_llm_key_default(credential_id: UUID, principal: Owner, request: Request,
                               db=Depends(get_db)) -> LlmKeyOut:
    try:
        view = await set_default_llm_key(db, tenant_id=principal.tenant_id, user_id=principal.user_id,
                                         credential_id=credential_id, ip=client_ip(request))
    except NoLlmCredentialError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "active LLM key not found") from None
    return LlmKeyOut(**view.__dict__)


@router.delete("/llm/credentials/{credential_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["llm"])
async def delete_llm_key(credential_id: UUID, principal: Owner, request: Request, db=Depends(get_db)) -> None:
    """გაუქმება + კრიპტო-წაშლა (გასაღები აღარ აღდგება). პროვაიდერის მხარეს გასაღები თავად გააუქმეთ."""
    try:
        await revoke_llm_key(db, tenant_id=principal.tenant_id, user_id=principal.user_id,
                             credential_id=credential_id, ip=client_ip(request))
    except NoLlmCredentialError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "LLM key not found") from None


@router.get("/llm/usage", response_model=list[UsageOut], tags=["llm"])
async def llm_usage(principal: Member, since: date, until: date | None = None,
                    db=Depends(get_db)) -> list[UsageOut]:
    """BYOK ხარჯის გამჭვირვალობა: გამოძახებები და ტოკენები პროვაიდერის/მოდელის/მიზნის მიხედვით."""
    if until is not None and until < since:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "until must not be before since")
    return [UsageOut(**r.__dict__) for r in await usage_summary(db, principal.tenant_id, since=since, until=until)]


# ------------------------------------------------------------------ consents


class ConsentIn(BaseModel):
    kind: ConsentKind
    document_version: str = Field(min_length=1, max_length=64, description="თანხმობის ტექსტის ვერსია, მაგ. 2026-09")


class ConsentOut(BaseModel):
    id: UUID
    kind: ConsentKind
    document_version: str
    user_id: UUID
    granted_at: datetime


@router.get("/consents", response_model=list[ConsentOut], tags=["consents"])
async def get_consents(principal: Member, db=Depends(get_db)) -> list[ConsentOut]:
    return [ConsentOut(**c.__dict__) for c in await list_consents(db, principal.tenant_id)]


@router.post("/consents", response_model=ConsentOut, status_code=status.HTTP_201_CREATED, tags=["consents"])
async def post_consent(body: ConsentIn, principal: Owner, request: Request, db=Depends(get_db)) -> ConsentOut:
    """თანხმობას გასცემს მხოლოდ owner (კომპანიის უფლებამოსილი წარმომადგენელი)."""
    view = await grant_consent(db, tenant_id=principal.tenant_id, user_id=principal.user_id, kind=body.kind,
                               document_version=body.document_version, ip=client_ip(request))
    return ConsentOut(**view.__dict__)


@router.delete("/consents/{kind}", status_code=status.HTTP_204_NO_CONTENT, tags=["consents"])
async def delete_consent(kind: ConsentKind, principal: Owner, request: Request, db=Depends(get_db)) -> None:
    if not await revoke_consent(db, tenant_id=principal.tenant_id, user_id=principal.user_id, kind=kind,
                                ip=client_ip(request)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no active consent of this kind")


# ------------------------------------------------------------------ rs.ge SMS code (human-in-the-loop)


class OtpIn(BaseModel):
    code: str = Field(pattern=r"^\s*\d{4,8}\s*$")


@router.post("/rsge/otp/{challenge_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["rsge"])
async def submit_otp(challenge_id: str, body: OtpIn, principal: Staff, broker=Depends(get_otp_broker)) -> None:
    if not await broker.submit_code(challenge_id, body.code, tenant_id=str(principal.tenant_id)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "challenge expired, already answered or not found")


# ------------------------------------------------------------------ declarations


class DeclarationEventOut(BaseModel):
    from_status: str | None
    to_status: str
    actor_type: str
    actor_id: UUID | None
    note: str | None
    created_at: datetime


class DeclarationOut(BaseModel):
    id: UUID
    taxpayer_id: UUID
    type: str
    period_start: date
    period_end: date
    due_date: date
    status: str
    is_zero: bool | None
    form_values: dict[str, Any] | None
    form_fingerprint: str | None
    draft_screenshot_key: str | None
    rule_decision: dict[str, Any] | None
    ai_decision: dict[str, Any] | None
    approved_by: UUID | None
    approved_at: datetime | None
    rs_receipt_number: str | None
    submitted_at: datetime | None
    last_error_code: str | None
    updated_at: datetime
    events: list[DeclarationEventOut] = []


class ApproveIn(BaseModel):
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$", description="ეკრანზე ნანახი ფორმის form_fingerprint")


def _declaration_out(view) -> DeclarationOut:
    return DeclarationOut(**{**view.__dict__, "events": [DeclarationEventOut(**e.__dict__) for e in view.events]})


@router.get("/declarations", response_model=list[DeclarationOut], tags=["declarations"])
async def get_declarations(principal: Member, db=Depends(get_db),
                           status_in: Annotated[list[str] | None, Query(alias="status")] = None,
                           limit: Annotated[int, Query(ge=1, le=200)] = 50) -> list[DeclarationOut]:
    return [_declaration_out(v) for v in await list_declarations(db, principal.tenant_id, statuses=status_in,
                                                                 limit=limit)]


@router.get("/declarations/{declaration_id}", response_model=DeclarationOut, tags=["declarations"])
async def get_one_declaration(declaration_id: UUID, principal: Member, db=Depends(get_db)) -> DeclarationOut:
    try:
        return _declaration_out(await get_declaration(db, principal.tenant_id, declaration_id))
    except DeclarationNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "declaration not found") from None


@router.post("/declarations/{declaration_id}/approve", response_model=DeclarationOut, tags=["declarations"])
async def post_approve_declaration(declaration_id: UUID, body: ApproveIn, principal: Staff, request: Request,
                                   db=Depends(get_db)) -> DeclarationOut:
    """ადამიანის დადასტურება rs.ge-ზე წარდგენამდე. fingerprint - ზუსტად ის ფორმა, რაც მომხმარებელმა ნახა."""
    try:
        view = await approve_declaration(db, tenant_id=principal.tenant_id, user_id=principal.user_id,
                                         declaration_id=declaration_id, fingerprint=body.fingerprint,
                                         ip=client_ip(request))
    except DeclarationNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "declaration not found") from None
    except DeclarationStateError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, {"code": exc.code, "message": str(exc)}) from None
    return _declaration_out(view)


# ------------------------------------------------------------------ tax calendar


class DeadlineOut(BaseModel):
    rule_code: str
    title_ka: str
    period: str
    due_date: date
    statutory_date: date
    shifted_to_business_day: bool
    requires_verification: bool


@router.get("/calendar/upcoming", response_model=list[DeadlineOut], tags=["calendar"])
async def calendar(principal: Member, regimes: Annotated[list[Regime], Query()],
                   horizon_days: Annotated[int, Query(ge=1, le=400)] = 45,
                   today: date | None = None) -> list[DeadlineOut]:
    items = upcoming_deadlines(today or date.today(), set(regimes), horizon_days=horizon_days)
    return [DeadlineOut(rule_code=d.rule.code, title_ka=d.rule.title_ka, period=str(d.period), due_date=d.due_date,
                        statutory_date=d.statutory_date, shifted_to_business_day=d.shifted,
                        requires_verification=d.rule.requires_verification) for d in items]


# ------------------------------------------------------------------ documents


@router.post("/documents/extract", tags=["documents"])
async def extract_document(principal: Staff, our_tin: Annotated[str, Query(pattern=r"^(\d{9}|\d{11})$")],
                           file: UploadFile = File(...), db=Depends(get_db), llm=Depends(get_router),
                           box=Depends(get_secret_box)) -> dict:
    """სინქრონული ვარიანტი დემოსთვის. prod: ფაილი -> S3, documents ჩანაწერი, დავალება რიგში."""
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    async with db.tenant_tx(principal.tenant_id) as conn:
        if not await has_active_consent(conn, ConsentKind.AI_PROCESSING):
            # პერსონალური მონაცემები საზღვარგარეთ (LLM პროვაიდერთან) თანხმობის გარეშე არ იგზავნება
            raise HTTPException(status.HTTP_409_CONFLICT, {
                "code": "consent_required", "kind": ConsentKind.AI_PROCESSING.value,
                "message": "the organisation owner must accept AI processing first"})
    try:
        creds = await load_default_credentials(db, box, principal.tenant_id)
    except NoLlmCredentialError:
        raise HTTPException(status.HTTP_409_CONFLICT, "add an LLM API key first") from None

    digest = hashlib.sha256(data).digest()
    try:
        processed = await AIAccountant(llm).process_document(creds, data, file.filename or "document",
                                                             our_tin=our_tin)
    except UnsupportedDocumentError as exc:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(exc)) from None
    except LLMError as exc:
        await record_llm_call(db, tenant_id=principal.tenant_id, creds=creds, purpose="extract_document",
                              input_sha256=digest, error=exc)
        raise _llm_http_error(exc, stored_key=True) from None

    extraction = processed.extraction.model_dump(mode="json")
    call_id = await record_llm_call(db, tenant_id=principal.tenant_id, creds=creds, purpose="extract_document",
                                    input_sha256=digest, result=processed.llm, output=extraction)
    return {
        "extraction": extraction,
        "validation": {"errors": processed.validation.errors, "warnings": processed.validation.warnings},
        "needs_review": processed.needs_review,
        "llm": {"call_id": str(call_id), "provider": processed.llm.provider.value, "model": processed.llm.model,
                "input_tokens": processed.llm.usage.input_tokens, "output_tokens": processed.llm.usage.output_tokens},
    }


# ------------------------------------------------------------------ audit


class AuditVerifyOut(BaseModel):
    ok: bool
    checked: int
    first_broken_seq: int | None
    last_hash: str | None = Field(description="ჩაიწერეთ გარე (WORM) საცავში - ბოლო ჩანაწერების წაშლის აღმოსაჩენად")


@router.get("/audit/verify", response_model=AuditVerifyOut, tags=["audit"])
async def audit_verify(principal: Owner, db=Depends(get_db)) -> AuditVerifyOut:
    async with db.tenant_tx(principal.tenant_id) as conn:
        result = await verify_audit_chain(conn)
    return AuditVerifyOut(ok=result.ok, checked=result.checked, first_broken_seq=result.first_broken_seq,
                          last_hash=result.last_hash)
