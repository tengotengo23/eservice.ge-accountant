"""დეკლარაციები: სია, დეტალები (სტატუსების ისტორიით) და ადამიანის დადასტურება.

დადასტურება = თანხმობა კონკრეტულ, ნანახ ფორმაზე. კლიენტი აგზავნის იმ fingerprint-ს, რომელიც
ეკრანზე ნახა; თუ ამასობაში worker-მა ფორმა ხელახლა მოამზადა (სხვა fingerprint), დადასტურება
უარყოფილია - მომხმარებელმა ახალი ვერსია უნდა ნახოს. DB-ის CHECK და ტრიგერი იგივეს მეორედ იცავს.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

import psycopg

from ..db.database import Database, audit

APPROVABLE = ("draft_prepared", "awaiting_approval")
_COLUMNS = ("id, taxpayer_id, type::text AS type, period_start, period_end, due_date, status::text AS status,"
            " is_zero, form_values, form_fingerprint, draft_screenshot_key, rule_decision, ai_decision,"
            " approved_by, approved_at, approved_fingerprint, rs_receipt_number, submitted_at, last_error_code,"
            " updated_at")


class DeclarationNotFoundError(LookupError):
    pass


class DeclarationStateError(ValueError):
    """დეკლარაცია არ არის დასადასტურებელ მდგომარეობაში, ან ფორმა შეიცვალა."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DeclarationEvent:
    from_status: str | None
    to_status: str
    actor_type: str
    actor_id: UUID | None
    note: str | None
    created_at: datetime


@dataclass(frozen=True)
class DeclarationView:
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
    approved_fingerprint: str | None
    rs_receipt_number: str | None
    submitted_at: datetime | None
    last_error_code: str | None
    updated_at: datetime
    events: tuple[DeclarationEvent, ...] = ()


async def list_declarations(db: Database, tenant_id: UUID, *, statuses: list[str] | None = None,
                            limit: int = 50) -> list[DeclarationView]:
    async with db.tenant_tx(tenant_id) as conn:
        cur = await conn.execute(
            f"SELECT {_COLUMNS} FROM declarations WHERE (%s::text[] IS NULL OR status::text = ANY(%s::text[]))"
            " ORDER BY due_date, type LIMIT %s", (statuses, statuses, limit))
        return [DeclarationView(**r) for r in await cur.fetchall()]


async def get_declaration(db: Database, tenant_id: UUID, declaration_id: UUID) -> DeclarationView:
    async with db.tenant_tx(tenant_id) as conn:
        row = await (await conn.execute(f"SELECT {_COLUMNS} FROM declarations WHERE id = %s",
                                        (declaration_id,))).fetchone()
        if row is None:
            raise DeclarationNotFoundError(str(declaration_id))
        events = await (await conn.execute(
            "SELECT from_status::text AS from_status, to_status::text AS to_status, actor_type::text AS actor_type,"
            " actor_id, note, created_at FROM declaration_events WHERE declaration_id = %s ORDER BY id",
            (declaration_id,))).fetchall()
    return DeclarationView(**row, events=tuple(DeclarationEvent(**e) for e in events))


async def approve_declaration(db: Database, *, tenant_id: UUID, user_id: UUID, declaration_id: UUID,
                              fingerprint: str, ip: str | None = None) -> DeclarationView:
    async with db.tenant_tx(tenant_id, actor_type="user", actor_id=user_id) as conn:
        row = await (await conn.execute(
            "SELECT status::text AS status, form_fingerprint, type::text AS type, period_start"
            " FROM declarations WHERE id = %s FOR UPDATE", (declaration_id,))).fetchone()
        if row is None:
            raise DeclarationNotFoundError(str(declaration_id))
        if row["status"] not in APPROVABLE:
            raise DeclarationStateError("not_approvable", f"declaration is {row['status']}, cannot be approved")
        if not row["form_fingerprint"] or not hmac.compare_digest(row["form_fingerprint"], fingerprint):
            raise DeclarationStateError("form_changed", "the form changed since you viewed it; review it again")
        await conn.execute(
            "UPDATE declarations SET status = 'approved', approved_by = %s, approved_at = now(),"
            " approved_fingerprint = form_fingerprint WHERE id = %s", (user_id, declaration_id))
        await audit(conn, tenant_id=tenant_id, actor_type="user", actor_id=user_id, action="declaration.approved",
                    entity_type="declaration", entity_id=str(declaration_id), ip=ip,
                    details={"fingerprint": fingerprint, "type": row["type"],
                             "period_start": row["period_start"].isoformat()})
    return await get_declaration(db, tenant_id, declaration_id)


class DeclarationExistsError(ValueError):
    pass


async def create_declaration(db: Database, *, tenant_id: UUID, user_id: UUID, taxpayer_id: UUID, dtype: str,
                             period: str, ip: str | None = None) -> DeclarationView:
    """ხელით შექმნა (დამგეგმავის გარდა). ვადა კალენდრიდან, სამუშაო დღეზე გადატანით."""
    from ..accounting.models import DeclarationType
    from .planning import declaration_period

    dates = declaration_period(DeclarationType(dtype), period)
    try:
        async with db.tenant_tx(tenant_id, actor_id=user_id) as conn:
            row = await (await conn.execute(
                "INSERT INTO declarations (tenant_id, taxpayer_id, type, period_start, period_end, due_date)"
                " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (tenant_id, taxpayer_id, dtype, dates.period_start, dates.period_end, dates.due_date))).fetchone()
            await audit(conn, tenant_id=tenant_id, actor_type="user", actor_id=user_id, action="declaration.created",
                        entity_type="declaration", entity_id=str(row["id"]), ip=ip,
                        details={"type": dtype, "period": period, "taxpayer_id": str(taxpayer_id)})
    except psycopg.errors.UniqueViolation:
        raise DeclarationExistsError(f"{dtype} {period} already exists for this taxpayer") from None
    except psycopg.errors.ForeignKeyViolation:
        raise DeclarationNotFoundError(f"taxpayer {taxpayer_id} not found") from None
    return await get_declaration(db, tenant_id, row["id"])


def job_key(action: str, view: DeclarationView) -> str:
    """ერთი და იგივე მდგომარეობიდან ერთი დავალება (ორმაგი დაჭერა/retry); სტატუსის ცვლილების შემდეგ - ახალი."""
    return f"{action}:{view.id}:{int(view.updated_at.timestamp() * 1_000_000)}"
