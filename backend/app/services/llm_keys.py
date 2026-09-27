"""მომხმარებლის LLM API Key-ის რეგისტრაცია (BYOK) და worker-ისთვის გაშიფვრა.

ნაკადი: გასაღების შემოწმება პროვაიდერთან (/models) -> მოდელის არჩევა -> დაშიფვრა (AAD = tenant +
ჩანაწერის id) -> შენახვა -> აუდიტი. სრული გასაღები API-დან უკან აღარასდროს ბრუნდება.
"""

from __future__ import annotations

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


def _view(row: dict) -> LlmCredentialView:
    return LlmCredentialView(id=row["id"], provider=Provider(row["provider"]), model=row["model"],
                             key_hint=row["key_hint"], is_default=row["is_default"], status=row["status"],
                             created_at=row["created_at"])


async def register_llm_key(db: Database, router: LLMRouter, box: SecretBox, *, tenant_id: UUID, user_id: UUID,
                           provider: Provider, api_key: str, model: str | None = None,
                           make_default: bool = True) -> tuple[LlmCredentialView, list[str]]:
    api_key = api_key.strip()
    available = await router.validate_key(provider, api_key)  # LLMAuthError -> 400 API-ში
    chosen = model or default_model_for(provider)
    if available and chosen not in available:
        raise ModelNotAvailableError(chosen, available)

    cred_id = uuid4()
    secret = box.encrypt(api_key, SecretContext(str(tenant_id), PURPOSE, str(cred_id)))
    async with db.tenant_tx(tenant_id, actor_id=user_id) as conn:
        if make_default:
            await conn.execute("UPDATE llm_credentials SET is_default = false WHERE is_default")
        cur = await conn.execute(
            """INSERT INTO llm_credentials (id, tenant_id, provider, ciphertext, wrapped_dek, kek_id, key_hint,
                                            model, is_default, last_validated_at, created_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now(), %s)
               RETURNING id, provider, model, key_hint, is_default, status, created_at""",
            (cred_id, tenant_id, provider.value, secret.ciphertext, secret.wrapped_dek, secret.kek_id,
             key_hint(api_key), chosen, make_default, user_id),
        )
        row = await cur.fetchone()
        await audit(conn, tenant_id=tenant_id, actor_type="user", actor_id=user_id, action="llm_key.created",
                    entity_type="llm_credential", entity_id=str(cred_id),
                    details={"provider": provider.value, "model": chosen, "key_hint": row["key_hint"]})
    return _view(row), available


async def list_llm_keys(db: Database, tenant_id: UUID) -> list[LlmCredentialView]:
    async with db.tenant_tx(tenant_id) as conn:
        cur = await conn.execute(
            "SELECT id, provider, model, key_hint, is_default, status, created_at FROM llm_credentials"
            " WHERE status <> 'revoked' ORDER BY created_at DESC")
        return [_view(r) for r in await cur.fetchall()]


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
    api_key = box.decrypt(secret, SecretContext(str(tenant_id), PURPOSE, str(row["id"])))
    return LLMCredentials(Provider(row["provider"]), api_key=api_key, model=row["model"])
