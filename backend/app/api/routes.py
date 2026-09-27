"""REST API (v1). მძიმე ოპერაციები (rs.ge, დიდი დოკუმენტები) production-ში რიგში (queue) იგზავნება."""

from __future__ import annotations

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Path, Query, UploadFile, status
from pydantic import BaseModel, Field, SecretStr

from ..accounting.ai_accountant import AIAccountant
from ..accounting.documents import MAX_UPLOAD_BYTES, DocumentTooLargeError, UnsupportedDocumentError
from ..accounting.tax_calendar import Regime, upcoming_deadlines
from ..llm import LLMAuthError, LLMError, LLMInputTooLargeError, LLMRefusalError, Provider
from ..llm.router import default_model_for
from ..services.llm_keys import (
    ModelNotAvailableError,
    NoLlmCredentialError,
    list_llm_keys,
    load_default_credentials,
    register_llm_key,
    revoke_llm_key,
    set_default_llm_key,
)
from .deps import STAFF_ROLES, Principal, get_db, get_otp_broker, get_router, get_secret_box, require_role

router = APIRouter(prefix="/v1")

Owner = Annotated[Principal, Depends(require_role("owner"))]
Member = Annotated[Principal, Depends(require_role("owner", "accountant", "viewer"))]
Staff = Annotated[Principal, Depends(require_role(*STAFF_ROLES))]


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
    available_models: list[str] | None = None


@router.get("/llm/providers", response_model=list[ProviderInfo])
async def providers() -> list[ProviderInfo]:
    return [ProviderInfo(id=p, default_model=default_model_for(p)) for p in Provider]


@router.post("/llm/credentials", response_model=LlmKeyOut, status_code=status.HTTP_201_CREATED)
async def create_llm_key(body: CreateLlmKey, principal: Owner, db=Depends(get_db), llm=Depends(get_router),
                         box=Depends(get_secret_box)) -> LlmKeyOut:
    try:
        view, available = await register_llm_key(
            db, llm, box, tenant_id=principal.tenant_id, user_id=principal.user_id, provider=body.provider,
            api_key=body.api_key.get_secret_value(), model=body.model, make_default=body.make_default)
    except LLMAuthError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "API key rejected by the provider") from None
    except ModelNotAvailableError as exc:
        raise HTTPException(422,
                            {"message": str(exc), "available_models": exc.available}) from None
    except LLMError:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "provider unavailable, try again later") from None
    return LlmKeyOut(**view.__dict__, available_models=available)


@router.get("/llm/credentials", response_model=list[LlmKeyOut])
async def get_llm_keys(principal: Member, db=Depends(get_db)) -> list[LlmKeyOut]:
    return [LlmKeyOut(**v.__dict__) for v in await list_llm_keys(db, principal.tenant_id)]


@router.post("/llm/credentials/{credential_id}/default", response_model=LlmKeyOut)
async def make_llm_key_default(credential_id: UUID, principal: Owner, db=Depends(get_db)) -> LlmKeyOut:
    try:
        view = await set_default_llm_key(db, tenant_id=principal.tenant_id, user_id=principal.user_id,
                                         credential_id=credential_id)
    except NoLlmCredentialError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "active LLM key not found") from None
    return LlmKeyOut(**view.__dict__)


@router.delete("/llm/credentials/{credential_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_llm_key(credential_id: UUID, principal: Owner, db=Depends(get_db)) -> None:
    """გაუქმება შეუქცევადია: დაშიფრული გასაღები იშლება (crypto-shredding), ჩანაწერი რჩება აუდიტისთვის."""
    try:
        await revoke_llm_key(db, tenant_id=principal.tenant_id, user_id=principal.user_id,
                             credential_id=credential_id)
    except NoLlmCredentialError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "LLM key not found") from None


# ------------------------------------------------------------------ rs.ge SMS code (human-in-the-loop)


class OtpIn(BaseModel):
    code: str = Field(pattern=r"^\s*\d{4,8}\s*$")


@router.post("/rsge/otp/{challenge_id}", status_code=status.HTTP_204_NO_CONTENT)
async def submit_otp(challenge_id: Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")], body: OtpIn, principal: Staff,
                     broker=Depends(get_otp_broker)) -> None:
    if not await broker.submit_code(challenge_id, body.code, tenant_id=str(principal.tenant_id)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "challenge expired or not found")


# ------------------------------------------------------------------ tax calendar


class DeadlineOut(BaseModel):
    rule_code: str
    title_ka: str
    period: str
    due_date: date
    statutory_date: date
    shifted_to_business_day: bool
    requires_verification: bool


@router.get("/calendar/upcoming", response_model=list[DeadlineOut])
async def calendar(principal: Member, regimes: Annotated[list[Regime], Query()],
                   horizon_days: Annotated[int, Query(ge=1, le=400)] = 45,
                   today: date | None = None) -> list[DeadlineOut]:
    items = upcoming_deadlines(today or date.today(), set(regimes), horizon_days=horizon_days)
    return [DeadlineOut(rule_code=d.rule.code, title_ka=d.rule.title_ka, period=str(d.period), due_date=d.due_date,
                        statutory_date=d.statutory_date, shifted_to_business_day=d.shifted,
                        requires_verification=d.rule.requires_verification) for d in items]


# ------------------------------------------------------------------ documents


@router.post("/documents/extract")
async def extract_document(principal: Staff, our_tin: Annotated[str, Query(pattern=r"^(\d{9}|\d{11})$")],
                           file: UploadFile = File(...), db=Depends(get_db), llm=Depends(get_router),
                           box=Depends(get_secret_box)) -> dict:
    """სინქრონული ვარიანტი დემოსთვის. prod: ფაილი -> S3, documents ჩანაწერი, დავალება რიგში."""
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, f"file exceeds {MAX_UPLOAD_BYTES // 1024 // 1024}MB")
    # our_tin განსაზღვრავს ვალიდაციას ("მყიდველი ჩვენ ვართ"); ის უნდა ეკუთვნოდეს ამ კომპანიას (RLS)
    async with db.tenant_tx(principal.tenant_id) as conn:
        cur = await conn.execute("SELECT 1 FROM taxpayers WHERE tin = %s", (our_tin,))
        if await cur.fetchone() is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "taxpayer with this TIN is not registered in your account")
    try:
        creds = await load_default_credentials(db, box, principal.tenant_id)
        processed = await AIAccountant(llm).process_document(creds, data, file.filename or "document",
                                                             our_tin=our_tin)
    except NoLlmCredentialError:
        raise HTTPException(status.HTTP_409_CONFLICT, "add an LLM API key first") from None
    except (DocumentTooLargeError, LLMInputTooLargeError) as exc:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, str(exc)) from None
    except UnsupportedDocumentError as exc:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(exc)) from None
    except LLMAuthError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "stored API key was rejected; update it") from None
    except LLMRefusalError:
        raise HTTPException(422, "the model declined; manual review needed") from None
    except LLMError:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "LLM provider error") from None
    return {
        "extraction": processed.extraction.model_dump(mode="json"),
        "validation": {"errors": processed.validation.errors, "warnings": processed.validation.warnings},
        "needs_review": processed.needs_review,
        "llm": {"provider": processed.llm.provider.value, "model": processed.llm.model,
                "input_tokens": processed.llm.usage.input_tokens, "output_tokens": processed.llm.usage.output_tokens},
    }
