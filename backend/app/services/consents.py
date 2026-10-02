"""თანხმობები (consents): ToS, AI-ით დამუშავება, rs.ge-ს მანდატი, სხვა LLM პროვაიდერზე fallback.

პერსონალური მონაცემების LLM პროვაიდერთან (აშშ/ევროკავშირი) გაგზავნა = საზღვარგარეთ გადაცემა, ამიტომ
`ai_processing` თანხმობის გარეშე დოკუმენტი LLM-ს არ ეგზავნება. ყოველი თანხმობა ინახავს ტექსტის
ვერსიას (რას დაეთანხმა), დროს და IP-ს; გაუქმება ჩანაწერს არ შლის (revoked_at).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from uuid import UUID

from psycopg import AsyncConnection

from ..db.database import Database, audit


class ConsentKind(str, Enum):
    TERMS_OF_SERVICE = "terms_of_service"
    AI_PROCESSING = "ai_processing"
    RSGE_AUTOMATION_MANDATE = "rsge_automation_mandate"
    LLM_FALLBACK_OTHER_PROVIDER = "llm_fallback_other_provider"


@dataclass(frozen=True)
class ConsentView:
    id: UUID
    kind: ConsentKind
    document_version: str
    user_id: UUID
    granted_at: datetime


def _view(row: dict) -> ConsentView:
    return ConsentView(id=row["id"], kind=ConsentKind(row["kind"]), document_version=row["document_version"],
                       user_id=row["user_id"], granted_at=row["granted_at"])


_COLUMNS = "id, kind, document_version, user_id, granted_at"


async def has_active_consent(conn: AsyncConnection, kind: ConsentKind) -> bool:
    cur = await conn.execute("SELECT 1 FROM consents WHERE kind = %s AND revoked_at IS NULL LIMIT 1", (kind.value,))
    return await cur.fetchone() is not None


async def list_consents(db: Database, tenant_id: UUID) -> list[ConsentView]:
    async with db.tenant_tx(tenant_id) as conn:
        cur = await conn.execute(f"SELECT {_COLUMNS} FROM consents WHERE revoked_at IS NULL ORDER BY granted_at")
        return [_view(r) for r in await cur.fetchall()]


async def grant_consent(db: Database, *, tenant_id: UUID, user_id: UUID, kind: ConsentKind, document_version: str,
                        ip: str | None = None) -> ConsentView:
    """idempotent: იგივე ვერსიაზე არსებული თანხმობა ბრუნდება; ახალი ვერსია ძველს ანაცვლებს."""
    async with db.tenant_tx(tenant_id, actor_id=user_id) as conn:
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('consent:' || %s::text || ':' || %s::text, 0))",
            (str(tenant_id), kind.value))
        cur = await conn.execute(
            f"SELECT {_COLUMNS} FROM consents WHERE kind = %s AND document_version = %s AND revoked_at IS NULL",
            (kind.value, document_version))
        if (existing := await cur.fetchone()) is not None:
            return _view(existing)
        await conn.execute("UPDATE consents SET revoked_at = now() WHERE kind = %s AND revoked_at IS NULL",
                           (kind.value,))
        cur = await conn.execute(
            f"INSERT INTO consents (tenant_id, user_id, kind, document_version, ip) VALUES (%s, %s, %s, %s, %s)"
            f" RETURNING {_COLUMNS}", (tenant_id, user_id, kind.value, document_version, ip))
        row = await cur.fetchone()
        await audit(conn, tenant_id=tenant_id, actor_type="user", actor_id=user_id, action="consent.granted",
                    entity_type="consent", entity_id=str(row["id"]), ip=ip,
                    details={"kind": kind.value, "document_version": document_version})
    return _view(row)


async def revoke_consent(db: Database, *, tenant_id: UUID, user_id: UUID, kind: ConsentKind,
                         ip: str | None = None) -> bool:
    async with db.tenant_tx(tenant_id, actor_id=user_id) as conn:
        cur = await conn.execute(
            "UPDATE consents SET revoked_at = now() WHERE kind = %s AND revoked_at IS NULL RETURNING id",
            (kind.value,))
        revoked = await cur.fetchall()
        if revoked:
            await audit(conn, tenant_id=tenant_id, actor_type="user", actor_id=user_id, action="consent.revoked",
                        entity_type="consent", entity_id=str(revoked[0]["id"]), ip=ip, details={"kind": kind.value})
    return bool(revoked)
