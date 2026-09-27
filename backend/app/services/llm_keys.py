"""მომხმარებლის LLM API Key-ის რეგისტრაცია (BYOK) და worker-ისთვის გაშიფვრა.

ნაკადი: გასაღების შემოწმება პროვაიდერთან (/models) -> მოდელის არჩევა -> დაშიფვრა (AAD = tenant +
ჩანაწერის id) -> შენახვა -> აუდიტი. სრული გასაღები API-დან უკან აღარასდროს ბრუნდება.

KMS-ის გამოძახება (wrap/unwrap) ქსელური და ბლოკირებადია (boto3), ამიტომ ეშვება ცალკე thread-ში -
სხვაგვარად ერთი ნელი KMS პასუხი მთელ event loop-ს (ყველა მომხმარებლის მოთხოვნას) გააჩერებდა.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from ..db.database import Database, audit
from ..llm import LLMCredentials, LLMRouter, Provider
from ..llm.router import default_model_for
from ..security.crypto import EncryptedSecret, SecretBox, SecretContext, key_hint

PURPOSE = "llm_api_key"


class ModelNotAvailableError(ValueError):
    def __init__(self, model: str, available: list[str]):
        super().__init__(f"model {model!r} is not available for this API key")
        self.model = model
        self.available = available


class NoLlmCredentialError(LookupError):
    pass


@dataclass(frozen=True)
class LlmCredentialView:
    id: UUID
    provider: Provider
    model: str
    key_hint: str
    is_default: bool
    status: str
    created_at: datetime


_VIEW_COLUMNS = "id, provider, model, key_hint, is_default, status, created_at"


def _view(row: dict) -> LlmCredentialView:
    return LlmCredentialView(id=row["id"], provider=Provider(row["provider"]), model=row["model"],
                             key_hint=row["key_hint"], is_default=row["is_default"], status=row["status"],
                             created_at=row["created_at"])


async def _lock_tenant_defaults(conn, tenant_id: UUID) -> None:
    """ერთ tenant-ზე "ნაგულისხმევის" ცვლილებები რიგრიგობით (სხვაგვარად ორი პარალელური რეგისტრაცია
    llm_credentials_one_default უნიკალურ ინდექსს დაარღვევდა და მომხმარებელი 500-ს მიიღებდა)."""
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('llm_default:' || %s, 0))",
                       (str(tenant_id),))


async def register_llm_key(db: Database, router: LLMRouter, box: SecretBox, *, tenant_id: UUID, user_id: UUID,
                           provider: Provider, api_key: str, model: str | None = None,
                           make_default: bool = True) -> tuple[LlmCredentialView, list[str]]:
    api_key = api_key.strip()
    available = await router.validate_key(provider, api_key)  # LLMAuthError -> 400 API-ში
    chosen = model or default_model_for(provider)
    if available and chosen not in available:
        raise ModelNotAvailableError(chosen, available)

    cred_id = uuid4()
    secret = await asyncio.to_thread(box.encrypt, api_key, SecretContext(str(tenant_id), PURPOSE, str(cred_id)))
    async with db.tenant_tx(tenant_id, actor_id=user_id) as conn:
        await _lock_tenant_defaults(conn, tenant_id)
        cur = await conn.execute("SELECT 1 FROM llm_credentials WHERE is_default AND status = 'active'")
        # პირველი აქტიური გასაღები ყოველთვის ნაგულისხმევია - სხვაგვარად დოკუმენტების დამუშავება ვერ დაიწყება
        is_default = make_default or await cur.fetchone() is None
        if is_default:
            await conn.execute("UPDATE llm_credentials SET is_default = false WHERE is_default")
        cur = await conn.execute(
            f"""INSERT INTO llm_credentials (id, tenant_id, provider, ciphertext, wrapped_dek, kek_id, key_hint,
                                             model, is_default, last_validated_at, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now(), %s)
                RETURNING {_VIEW_COLUMNS}""",
            (cred_id, tenant_id, provider.value, secret.ciphertext, secret.wrapped_dek, secret.kek_id,
             key_hint(api_key), chosen, is_default, user_id),
        )
        row = await cur.fetchone()
        await audit(conn, tenant_id=tenant_id, actor_type="user", actor_id=user_id, action="llm_key.created",
                    entity_type="llm_credential", entity_id=str(cred_id),
                    details={"provider": provider.value, "model": chosen, "key_hint": row["key_hint"],
                             "is_default": is_default})
    return _view(row), available


async def list_llm_keys(db: Database, tenant_id: UUID) -> list[LlmCredentialView]:
    async with db.tenant_tx(tenant_id) as conn:
        cur = await conn.execute(
            f"SELECT {_VIEW_COLUMNS} FROM llm_credentials WHERE status <> 'revoked' ORDER BY created_at DESC")
        return [_view(r) for r in await cur.fetchall()]


async def set_default_llm_key(db: Database, *, tenant_id: UUID, user_id: UUID,
                              credential_id: UUID) -> LlmCredentialView:
    async with db.tenant_tx(tenant_id, actor_id=user_id) as conn:
        await _lock_tenant_defaults(conn, tenant_id)
        cur = await conn.execute("SELECT provider FROM llm_credentials WHERE id = %s AND status = 'active'",
                                 (credential_id,))
        if await cur.fetchone() is None:
            raise NoLlmCredentialError("active LLM key not found")
        await conn.execute("UPDATE llm_credentials SET is_default = false WHERE is_default AND id <> %s",
                           (credential_id,))
        cur = await conn.execute(
            f"UPDATE llm_credentials SET is_default = true WHERE id = %s RETURNING {_VIEW_COLUMNS}", (credential_id,))
        row = await cur.fetchone()
        await audit(conn, tenant_id=tenant_id, actor_type="user", actor_id=user_id, action="llm_key.made_default",
                    entity_type="llm_credential", entity_id=str(credential_id),
                    details={"provider": row["provider"], "model": row["model"]})
    return _view(row)


async def revoke_llm_key(db: Database, *, tenant_id: UUID, user_id: UUID, credential_id: UUID) -> None:
    """გაუქმება = crypto-shredding: შიფრტექსტი და wrapped DEK იშლება, გასაღების აღდგენა შეუძლებელია.

    ნაგულისხმევი გასაღების გაუქმებისას სხვა გასაღები ავტომატურად არ ხდება ნაგულისხმევი - ეს შეიძლება
    სხვა პროვაიდერი იყოს, ხოლო მონაცემების სხვა პროვაიდერთან გაგზავნას მომხმარებლის ცალსახა არჩევანი სჭირდება.
    """
    async with db.tenant_tx(tenant_id, actor_id=user_id) as conn:
        await _lock_tenant_defaults(conn, tenant_id)
        cur = await conn.execute(
            "UPDATE llm_credentials SET status = 'revoked', is_default = false, ciphertext = ''::bytea,"
            " wrapped_dek = ''::bytea, rotated_at = now() WHERE id = %s AND status <> 'revoked'"
            " RETURNING provider, key_hint", (credential_id,))
        row = await cur.fetchone()
        if row is None:
            raise NoLlmCredentialError("LLM key not found")
        await audit(conn, tenant_id=tenant_id, actor_type="user", actor_id=user_id, action="llm_key.revoked",
                    entity_type="llm_credential", entity_id=str(credential_id),
                    details={"provider": row["provider"], "key_hint": row["key_hint"]})


async def load_default_credentials(db: Database, box: SecretBox, tenant_id: UUID) -> LLMCredentials:
    """worker-ისთვის: გაშიფრული გასაღები მხოლოდ მეხსიერებაში, დავალების ხანგრძლივობით."""
    async with db.tenant_tx(tenant_id, actor_type="worker") as conn:
        cur = await conn.execute(
            "SELECT id, provider, model, ciphertext, wrapped_dek, kek_id FROM llm_credentials"
            " WHERE is_default AND status = 'active'")
        row = await cur.fetchone()
    if row is None:
        raise NoLlmCredentialError("tenant has no active default LLM API key")
    secret = EncryptedSecret(ciphertext=bytes(row["ciphertext"]), wrapped_dek=bytes(row["wrapped_dek"]),
                             kek_id=row["kek_id"])
    api_key = await asyncio.to_thread(box.decrypt, secret, SecretContext(str(tenant_id), PURPOSE, str(row["id"])))
    return LLMCredentials(Provider(row["provider"]), api_key=api_key, model=row["model"])
