"""Run with `uvicorn app.api.main:create_app --factory`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.errors import install_error_handlers
from app.api.routes import health, jobs, sessions
from app.core.config import ApiSettings, get_api_settings
from app.core.logging import configure_logging
from app.core.redis import create_redis


def create_app(settings: ApiSettings | None = None) -> FastAPI:
    settings = settings or get_api_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.redis = create_redis(settings.redis_url)
        try:
            yield
        finally:
            await app.state.redis.aclose()

    app = FastAPI(title="AtlasVision Inference API", lifespan=lifespan)
    app.state.settings = settings

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["X-API-Key", "Content-Type"],
        )

    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(jobs.router)
    app.include_router(sessions.router)
    return app
