"""FastAPI აპლიკაცია.  გაშვება:  uvicorn app.main:app --reload"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api.routes import router as v1
from .config import get_settings
from .db.database import Database
from .llm import LLMRouter
from .rsge.otp import RedisOtpBroker
from .security.crypto import SecretBox, build_key_provider
from .security.redaction import install_redaction


@asynccontextmanager
async def lifespan(app: FastAPI):
    import redis.asyncio as redis

    settings = get_settings()
    install_redaction()
    app.state.db = Database(settings.database_url)
    await app.state.db.open()
    app.state.redis = redis.from_url(settings.redis_url)
    app.state.otp_broker = RedisOtpBroker(app.state.redis)
    app.state.llm_router = LLMRouter()
    app.state.secret_box = SecretBox(build_key_provider(settings))
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
        return {"status": "ok"}

    return app


app = create_app()
