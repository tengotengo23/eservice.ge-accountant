"""PostgreSQL წვდომა (psycopg 3, async pool). ყოველი ტრანზაქცია კონკრეტული tenant-ის კონტექსტშია (RLS)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


class Database:
    def __init__(self, dsn: str, *, set_role: str | None = None, min_size: int = 1, max_size: int = 10):
        """set_role - თუ DSN-ის მომხმარებელი სქემის მფლობელია (dev/test), ტრანზაქციაში გადავდივართ
        შეზღუდულ როლზე, რათა RLS იმოქმედოს. prod-ში DSN-ის მომხმარებელი თავადაა ai_accountant_app."""
        self._pool = AsyncConnectionPool(dsn, min_size=min_size, max_size=max_size, open=False,
                                         kwargs={"row_factory": dict_row})
        self._set_role = set_role

    async def open(self) -> None:
        await self._pool.open(wait=True)

    async def close(self) -> None:
        await self._pool.close()

    async def ping(self) -> None:
        """readiness probe-ისთვის: კავშირი ცოცხალია? (tenant-ის კონტექსტი არ სჭირდება)"""
        async with self._pool.connection(timeout=5) as conn:
            await conn.execute("SELECT 1")

    @asynccontextmanager
    async def tenant_tx(self, tenant_id: UUID | str, *, actor_type: str = "user",
                        actor_id: UUID | str | None = None) -> AsyncIterator[AsyncConnection]:
        async with self._pool.connection() as conn:
            async with conn.transaction():
                if self._set_role:
                    await conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(self._set_role)))
                await conn.execute(
                    "SELECT set_config('app.tenant_id', %s, true), set_config('app.actor_type', %s, true),"
                    " set_config('app.actor_id', %s, true)",
                    (str(tenant_id), actor_type, str(actor_id) if actor_id else ""),
                )
                yield conn


async def audit(conn: AsyncConnection, *, tenant_id, actor_type: str, actor_id, action: str,
                entity_type: str | None = None, entity_id: str | None = None, details: dict | None = None) -> None:
    from psycopg.types.json import Jsonb

    await conn.execute(
        "INSERT INTO audit_log (tenant_id, actor_type, actor_id, action, entity_type, entity_id, details)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (tenant_id, actor_type, actor_id, action, entity_type, entity_id, Jsonb(details or {})),
    )
