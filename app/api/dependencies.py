from __future__ import annotations

from fastapi import Request
from redis.asyncio import Redis

from app.core.config import ApiSettings


def get_settings(request: Request) -> ApiSettings:
    return request.app.state.settings


def get_redis(request: Request) -> Redis:
    return request.app.state.redis
