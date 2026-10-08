from __future__ import annotations

import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from redis.asyncio import Redis

from app.api.dependencies import get_redis, get_settings
from app.api.limits import ReadPrincipal
from app.core.config import ApiSettings
from app.core.heartbeat import ModelState, fresh, model_state, read_heartbeats
from app.core.schemas import WorkerState

router = APIRouter(prefix="/v1", tags=["model"])


class ModelView(BaseModel):
    state: ModelState
    model: dict[str, Any] | None
    error_code: str | None


@router.get("/model", response_model=ModelView)
async def model(
    principal: ReadPrincipal,
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[ApiSettings, Depends(get_settings)],
) -> ModelView:
    now = time.time()
    beats = await read_heartbeats(redis)
    live = fresh(beats, now=now, stale_after=settings.heartbeat_stale_after)
    ready = next((b for b in live if b.state is WorkerState.READY), None)
    failed = next((b for b in live if b.state is WorkerState.FAILED), None)
    state = model_state(beats, now=now, stale_after=settings.heartbeat_stale_after)
    return ModelView(
        state=state,
        model=ready.model if ready else None,
        error_code=failed.error_code if failed and state is ModelState.FAILED else None,
    )
