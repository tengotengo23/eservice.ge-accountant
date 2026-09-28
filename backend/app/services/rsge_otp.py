"""rs.ge SMS კოდის მოთხოვნები UI-სთვის: worker-ი challenge-ს DB-ში იწერს, UI აჩვენებს, მომხმარებელი პასუხობს.

კოდი DB-ში არასდროს ინახება (მხოლოდ Redis-ში, წუთებით) - აქ მხოლოდ მეტამონაცემებია: ვინ, როდის, რომელ
გაშვებაზე უპასუხა.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from ..db.database import Database, audit


@dataclass(frozen=True)
class PendingChallenge:
    id: str
    phone_hint: str | None
    created_at: datetime
    expires_at: datetime
    automation_run_id: UUID
    run_kind: str
    declaration_id: UUID | None
    taxpayer_id: UUID


async def list_pending_challenges(db: Database, tenant_id: UUID) -> list[PendingChallenge]:
    async with db.tenant_tx(tenant_id) as conn:
        rows = await (await conn.execute(
            "SELECT c.id, c.phone_hint, c.created_at, c.expires_at, c.automation_run_id, r.kind::text AS run_kind,"
            " r.declaration_id, r.taxpayer_id FROM otp_challenges c"
            " JOIN automation_runs r ON r.id = c.automation_run_id"
            " WHERE c.status = 'pending' AND c.expires_at > now() ORDER BY c.created_at")).fetchall()
    return [PendingChallenge(**r) for r in rows]


async def mark_challenge_answered(db: Database, *, tenant_id: UUID, user_id: UUID, challenge_id: str,
                                  ip: str | None = None) -> None:
    async with db.tenant_tx(tenant_id, actor_id=user_id) as conn:
        cur = await conn.execute(
            "UPDATE otp_challenges SET status = 'answered', answered_by = %s, answered_at = now()"
            " WHERE id = %s AND status = 'pending'", (user_id, challenge_id))
        if cur.rowcount:
            await audit(conn, tenant_id=tenant_id, actor_type="user", actor_id=user_id, action="rsge.otp_answered",
                        entity_type="otp_challenge", entity_id=challenge_id, ip=ip)
