"""Run with `uvicorn app.api.main:create_app --factory`."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.auth import EnvKeyStore
from app.api.errors import install_error_handlers
from app.api.middleware import BodySizeLimit
from app.api.routes import health, jobs, model, sessions
from app.core.config import ApiSettings, get_api_settings
from app.core.logging import configure_logging
from app.core.redis import create_redis

logger = logging.getLogger(__name__)


def create_app(settings: ApiSettings | None = None) -> FastAPI:
    settings = settings or get_api_settings()
    configure_logging(settings.log_level)
    if not settings.api_keys:
        raise RuntimeError("API_KEYS is empty: set at least one key; the API refuses to start.")
    if os.environ.get("HF_TOKEN"):
        logger.warning("HF_TOKEN is set in the API's environment; it belongs to the worker only.")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.redis = create_redis(settings.redis_url)
        try:
            yield
        finally:
            await app.state.redis.aclose()

    app = FastAPI(title="AtlasVision Inference API", lifespan=lifespan)
    app.state.settings = settings
    app.state.key_store = EnvKeyStore(settings.api_keys)
    app.add_middleware(
        BodySizeLimit, max_bytes=settings.max_request_bytes, key_store=app.state.key_store
    )

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["X-API-Key", "X-Client-Id", "Content-Type"],
        )

    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(jobs.router)
    app.include_router(sessions.router)
    app.include_router(model.router)
    return app
