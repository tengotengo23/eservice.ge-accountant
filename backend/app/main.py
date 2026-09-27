"""FastAPI აპლიკაცია.  გაშვება:  uvicorn app.main:app --reload"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .api.routes import router as v1
from .config import check_startup_settings, get_settings
from .db.database import Database
from .llm import LLMRouter
from .rsge.otp import RedisOtpBroker
from .security.auth import build_token_verifier
from .security.crypto import SecretBox, build_key_provider
from .security.redaction import install_redaction

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    import redis.asyncio as redis

    settings = get_settings()
    install_redaction()
    check_startup_settings(settings)
    # კონფიგურაციის შეცდომები (KEK, OIDC) - გაშვებისთანავე, სანამ DB/Redis-ს დავუკავშირდებით
    app.state.secret_box = SecretBox(build_key_provider(settings))
    app.state.token_verifier = build_token_verifier(settings)
    app.state.llm_router = LLMRouter()
    app.state.db = Database(settings.database_url)
    await app.state.db.open()
    app.state.redis = redis.from_url(settings.redis_url)
    app.state.otp_broker = RedisOtpBroker(app.state.redis)
    try:
        yield
    finally:
        await app.state.redis.aclose()
        await app.state.db.close()


def create_app(*, with_lifespan: bool = True) -> FastAPI:
    logging.basicConfig(level=logging.INFO)
    app = FastAPI(title="AI-ბუღალტერი API", version="0.1.0", lifespan=lifespan if with_lifespan else None)
    app.include_router(v1)

    @app.get("/health")
    async def health() -> dict:
        """liveness: პროცესი ცოცხალია (დამოკიდებულებებს არ ამოწმებს - მათი გათიშვა რესტარტით არ გასწორდება)."""
        return {"status": "ok"}

    @app.get("/ready")
    async def ready(request: Request) -> JSONResponse:
        """readiness: ტრაფიკის მიღება შეიძლება მხოლოდ მაშინ, როცა PostgreSQL და Redis ხელმისაწვდომია."""
        checks: dict[str, str] = {}
        for name, probe in (("postgres", lambda: request.app.state.db.ping()),
                            ("redis", lambda: request.app.state.redis.ping())):
            try:
                await probe()
                checks[name] = "ok"
            except Exception as exc:  # noqa: BLE001 - probe-ის ნებისმიერი ჩავარდნა = not ready
                log.warning("readiness check %s failed: %s", name, type(exc).__name__)
                checks[name] = "unavailable"
        ok = all(v == "ok" for v in checks.values())
        return JSONResponse({"status": "ok" if ok else "unavailable", "checks": checks},
                            status_code=200 if ok else 503)

    return app


app = create_app()
