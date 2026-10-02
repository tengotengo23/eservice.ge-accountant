"""LLM გამოძახებების აღრიცხვა (llm_calls): ხარჯი (ტოკენები), შეცდომები, შეყოვნება - პროვაიდერის მიხედვით.

BYOK-ში ხარჯს კლიენტი იხდის, ამიტომ მას უნდა ჰქონდეს გამჭვირვალე ანგარიში: რამდენი გამოძახება,
რომელ მოდელზე, რამდენი ტოკენი. შიგთავსს (prompt/ფაილი) არ ვინახავთ - მხოლოდ ჰეშს და ვალიდირებულ output-ს.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from ..db.database import Database
from ..llm import LLMCredentials, LLMError, StructuredResult
from ..llm.router import default_model_for


@dataclass(frozen=True)
class UsageRow:
    provider: str
    model: str
    purpose: str
    calls: int
    failed_calls: int
    input_tokens: int
    output_tokens: int


async def record_llm_call(db: Database, *, tenant_id: UUID, creds: LLMCredentials, purpose: str,
                          input_sha256: bytes | None = None, result: StructuredResult | None = None,
                          output: dict[str, Any] | None = None, error: LLMError | None = None) -> UUID:
    if result is not None:
        provider, model = result.provider, result.model
        # fallback-ის შემთხვევაში პასუხი სხვა პროვაიდერის (სხვა გასაღების) იყო
        credential_id = creds.credential_id if result.provider == creds.provider else None
        values = (result.usage.input_tokens, result.usage.output_tokens, result.attempts, result.latency_ms,
                  result.request_id, None)
    else:
        provider, model = creds.provider, creds.model or default_model_for(creds.provider)
        credential_id = creds.credential_id
        values = (None, None, 1, None, None, type(error).__name__ if error else "unknown")
    async with db.tenant_tx(tenant_id, actor_type="worker") as conn:
        cur = await conn.execute(
            "INSERT INTO llm_calls (tenant_id, llm_credential_id, provider, model, purpose, input_sha256, output,"
            " input_tokens, output_tokens, attempts, latency_ms, provider_request_id, error_code)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (tenant_id, credential_id, provider.value, model, purpose, input_sha256,
             Jsonb(output) if output is not None else None, *values))
        return (await cur.fetchone())["id"]


async def usage_summary(db: Database, tenant_id: UUID, *, since: date, until: date | None = None) -> list[UsageRow]:
    async with db.tenant_tx(tenant_id) as conn:
        cur = await conn.execute(
            "SELECT provider::text AS provider, model, purpose::text AS purpose, count(*) AS calls,"
            " count(*) FILTER (WHERE error_code IS NOT NULL) AS failed_calls,"
            " coalesce(sum(input_tokens), 0) AS input_tokens, coalesce(sum(output_tokens), 0) AS output_tokens"
            " FROM llm_calls WHERE created_at >= %s AND (%s::date IS NULL OR created_at < %s::date + 1)"
            " GROUP BY 1, 2, 3 ORDER BY 1, 2, 3", (since, until, until))
        return [UsageRow(**r) for r in await cur.fetchall()]
