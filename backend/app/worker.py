"""Worker (ARQ, Redis): rs.ge ავტომატიზაცია და დაგეგმვა.  გაშვება:  arq app.worker.WorkerSettings

* rs.ge დავალებები ავტომატურად არ მეორდება (max_tries=1): არასწორ პაროლზე/SMS-ზე ხელახალი ცდა
  ანგარიშს ბლოკავს. ხელახლა გაშვება - ადამიანის გადაწყვეტილებით (API-დან).
* job_id API-ში დედუპლიკაციისთვისაა: ორმაგი დაჭერა ორ ბრაუზერის სესიას ვერ გაუშვებს.
* production-ში worker ცალკე კონტეინერშია, egress მხოლოდ *.rs.ge-ზე (იხ. docs/ARCHITECTURE.md).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from arq import cron, func
from arq.connections import RedisSettings

from .config import get_settings
from .db.database import Database
from .rsge.otp import RedisOtpBroker
from .security.crypto import SecretBox, build_key_provider
from .security.redaction import install_redaction
from .services.jobs import PREPARE, SUBMIT
from .services.planning import plan_all_tenants
from .workflows.declaration_flow import FlowContext, FlowError, prepare_declaration, submit_declaration

log = logging.getLogger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("arq").propagate = False  # arq-ს საკუთარი handler-ი აქვს - ორმაგი ხაზების გარეშე
    install_redaction()
    db = Database(settings.database_url)
    await db.open()
    redis = ctx["redis"]  # ArqRedis (redis.asyncio.Redis) - იგივე კავშირი OTP-სთვის
    ctx["db"] = db
    ctx["flow"] = FlowContext(db=db, box=SecretBox(build_key_provider(settings)), settings=settings,
                              otp_factory=lambda notifier: RedisOtpBroker(redis, notifier=notifier))


async def shutdown(ctx: dict[str, Any]) -> None:
    if "db" in ctx:
        await ctx["db"].close()


async def _run(flow_fn, ctx: dict[str, Any], tenant_id: str, declaration_id: str) -> str:
    try:
        return await flow_fn(ctx["flow"], UUID(tenant_id), UUID(declaration_id))
    except FlowError as exc:
        # წინაპირობა არ სრულდება - ხელახლა ცდას აზრი არ აქვს; შედეგი ჩანს job-ის result-ში
        log.warning("%s %s skipped: %s", flow_fn.__name__, declaration_id, exc.code)
        return f"skipped:{exc.code}"


async def prepare_declaration_job(ctx: dict[str, Any], tenant_id: str, declaration_id: str) -> str:
    return await _run(prepare_declaration, ctx, tenant_id, declaration_id)


async def submit_declaration_job(ctx: dict[str, Any], tenant_id: str, declaration_id: str) -> str:
    return await _run(submit_declaration, ctx, tenant_id, declaration_id)


async def plan_declarations_job(ctx: dict[str, Any]) -> int:
    created = await plan_all_tenants(ctx["db"], today=datetime.now(ZoneInfo("Asia/Tbilisi")).date())
    log.info("planned %d new declarations", created)
    return created


class WorkerSettings:
    functions = [
        func(prepare_declaration_job, name=PREPARE, max_tries=1),
        func(submit_declaration_job, name=SUBMIT, max_tries=1),
    ]
    cron_jobs = [cron(plan_declarations_job, hour={8}, minute={17}, max_tries=1)]
    timezone = ZoneInfo("Asia/Tbilisi")  # საგადასახადო ვადები საქართველოს დროით
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    job_timeout = 20 * 60  # SMS-ის ლოდინი (3 წთ) + ბრაუზერი
    max_jobs = 4
    # შედეგს მხოლოდ დედუპლიკაციისთვის ვინახავთ (მდგომარეობა DB-შია). დიდი მნიშვნელობა გამოტოვებულ
    # დავალებას (მაგ. მანდატი აკლდა) იგივე job_id-ით ხელახლა გაშვებას დიდხანს დაბლოკავდა.
    keep_result = 60
    health_check_interval = 30
