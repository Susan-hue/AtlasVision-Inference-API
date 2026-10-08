from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel
from redis.asyncio import Redis

from app.api.dependencies import get_redis
from app.core.redis import ping

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    redis: Literal["ok", "unavailable"]


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={503: {"model": HealthResponse, "description": "Redis is unreachable"}},
)
async def health(redis: Annotated[Redis, Depends(get_redis)], response: Response) -> HealthResponse:
    if await ping(redis):
        return HealthResponse(status="ok", redis="ok")
    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(status="degraded", redis="unavailable")
