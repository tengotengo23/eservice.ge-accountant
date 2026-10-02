"""დეკლარაციების დაგეგმვა საგადასახადო კალენდრიდან: გადამხდელის რეჟიმები -> მოსალოდნელი დეკლარაციები.

ყოველდღიური scheduler (worker cron) თითოეული tenant-ისთვის ქმნის `planned` დეკლარაციებს უახლოესი
ვადებისთვის. იდემპოტენტურია: არსებულს (გაუქმებულის ჩათვლით - მომხმარებლის გადაწყვეტილებას ვცემთ
პატივს) ხელახლა არ ქმნის.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from ..accounting.models import DeclarationType
from ..accounting.tax_calendar import RULES, DeadlineRule, Regime, TaxPeriod, deadline_for, upcoming_deadlines
from ..db.database import Database


class UnsupportedPeriodError(ValueError):
    pass


@dataclass(frozen=True)
class DeclarationPeriod:
    period_start: date
    period_end: date
    due_date: date


def _rule_for(dtype: DeclarationType) -> DeadlineRule:
    for rule in RULES:
        if rule.declaration_type is dtype and rule.frequency == "monthly":
            return rule
    raise UnsupportedPeriodError(f"no monthly deadline rule for {dtype.value}")


def declaration_period(dtype: DeclarationType, period: str) -> DeclarationPeriod:
    """'2026-09' -> 2026-09-01 .. 2026-09-30, ვადა კალენდრიდან (სამუშაო დღეზე გადატანით)."""
    tp = TaxPeriod.parse(period)
    if tp.month is None:
        raise UnsupportedPeriodError(f"{dtype.value} is filed monthly; use YYYY-MM")
    deadline = deadline_for(_rule_for(dtype), tp)
    return DeclarationPeriod(date(tp.year, tp.month, 1), date(tp.year, tp.month, monthrange(tp.year, tp.month)[1]),
                             deadline.due_date)


async def plan_declarations(db: Database, tenant_id: UUID, *, today: date, horizon_days: int = 45) -> int:
    """აბრუნებს ახლად შექმნილი დეკლარაციების რაოდენობას."""
    created = 0
    async with db.tenant_tx(tenant_id, actor_type="system") as conn:
        taxpayers = await (await conn.execute("SELECT id, regimes::text[] AS regimes FROM taxpayers")).fetchall()
        for tp in taxpayers:
            regimes = {Regime(r) for r in tp["regimes"]}
            for dl in upcoming_deadlines(today, regimes, horizon_days=horizon_days):
                if dl.rule.declaration_type is None or dl.period.month is None:
                    continue  # წლიური/ინფორმაციული ვადები - მხოლოდ კალენდარში და შეხსენებებში
                start = date(dl.period.year, dl.period.month, 1)
                end = date(dl.period.year, dl.period.month, monthrange(dl.period.year, dl.period.month)[1])
                cur = await conn.execute(
                    "INSERT INTO declarations (tenant_id, taxpayer_id, type, period_start, period_end, due_date)"
                    " SELECT %s, %s, %s, %s, %s, %s WHERE NOT EXISTS (SELECT 1 FROM declarations"
                    "   WHERE taxpayer_id = %s AND type = %s AND period_start = %s)"
                    " ON CONFLICT (taxpayer_id, type, period_start) WHERE status <> 'cancelled' DO NOTHING",
                    (tenant_id, tp["id"], dl.rule.declaration_type.value, start, end, dl.due_date,
                     tp["id"], dl.rule.declaration_type.value, start))
                created += cur.rowcount
    return created


async def plan_all_tenants(db: Database, *, today: date, horizon_days: int = 45) -> int:
    total = 0
    for tenant_id in await db.system_tenant_ids():
        total += await plan_declarations(db, tenant_id, today=today, horizon_days=horizon_days)
    return total
