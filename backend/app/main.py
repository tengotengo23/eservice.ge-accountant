"""FastAPI აპლიკაცია.  გაშვება:  uvicorn app.main:app --reload"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api.middleware import BodySizeLimitMiddleware, RequestIdLogFilter, RequestIdMiddleware
from .api.routes import router as v1
from .config import Settings, get_settings
from .db.database import Database
from .llm import LLMRouter
from .rsge.otp import RedisOtpBroker
from .security.crypto import SecretBox, build_key_provider
from .security.oidc import OidcVerifier
from .security.redaction import install_redaction
from .services.jobs import ArqJobQueue

log = logging.getLogger(__name__)


def check_startup_settings(settings: Settings) -> None:
    """dev-ის შეღავათები (header-ებით "ავთენტიფიკაცია") სხვა გარემოში გაშვებასაც კი არ უნდა დაუშვას."""
    if settings.env != "dev" and settings.auth_mode != "oidc":
        raise RuntimeError(f"APP_ENV={settings.env} requires APP_AUTH_MODE=oidc")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from arq import create_pool
    from arq.connections import RedisSettings

    settings = get_settings()
    check_startup_settings(settings)
    install_redaction()  # uvicorn-ის handler-ები ამ დროისთვის უკვე შექმნილია
    app.state.db = Database(settings.database_url)
    await app.state.db.open()
    # ArqRedis = redis.asyncio.Redis + დავალებების რიგი: ერთი კავშირი OTP-ს, readiness-ს და worker-ის რიგს
    app.state.redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    app.state.otp_broker = RedisOtpBroker(app.state.redis)
    app.state.jobs = ArqJobQueue(app.state.redis)
    app.state.llm_router = LLMRouter()
    app.state.secret_box = SecretBox(build_key_provider(settings))
    if settings.auth_mode == "oidc":
        app.state.oidc = OidcVerifier(issuer=settings.oidc_issuer, audience=settings.oidc_audience,
                                      jwks_url=settings.oidc_jwks_url, algorithms=settings.oidc_algorithms,
                                      leeway_seconds=settings.oidc_leeway_seconds)
    try:
        yield
    finally:
        await app.state.redis.aclose()
        await app.state.db.close()


def _configure_logging() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s [req=%(request_id)s] %(message)s")
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, RequestIdLogFilter) for f in handler.filters):
            handler.addFilter(RequestIdLogFilter())
    install_redaction()


def create_app(*, with_lifespan: bool = True, settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    _configure_logging()
    app = FastAPI(title="AI-ბუღალტერი API", version="0.2.0", lifespan=lifespan if with_lifespan else None)
    app.include_router(v1)

    # add_middleware: ბოლოს დამატებული = ყველაზე გარე. request id -> CORS -> ზომის ლიმიტი -> აპი,
    # რათა 413 პასუხსაც ჰქონდეს CORS header-ები (ბრაუზერმა შეცდომა დაინახოს) და request id.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes)
    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=False,
                           allow_methods=["GET", "POST", "PUT", "DELETE"],
                           allow_headers=["Authorization", "Content-Type", "X-Tenant-Id", "X-Request-Id"],
                           expose_headers=["X-Request-Id"])
    app.add_middleware(RequestIdMiddleware)

    @app.get("/health", tags=["ops"])
    async def health() -> dict:
        """liveness: პროცესი ცოცხალია (დამოკიდებულებებს არ ამოწმებს)."""
        return {"status": "ok"}

    @app.get("/ready", tags=["ops"])
    async def ready(request: Request) -> JSONResponse:
        """readiness: PostgreSQL და Redis პასუხობენ - load balancer-ი მხოლოდ მზა ინსტანციაზე აგზავნის."""
        checks: dict[str, str] = {}
        state = request.app.state
        for name, probe in (("database", lambda: state.db.ping()), ("redis", lambda: state.redis.ping())):
            try:
                await probe()
                checks[name] = "ok"
            except Exception as exc:  # noqa: BLE001 - ნებისმიერი ჩავარდნა = "არ ვართ მზად"
                log.warning("readiness check %s failed: %s", name, type(exc).__name__)
                checks[name] = "unavailable"
        ok = all(v == "ok" for v in checks.values())
        return JSONResponse({"status": "ok" if ok else "unavailable", "checks": checks}, status_code=200 if ok else 503)

    return app


app = create_app()
