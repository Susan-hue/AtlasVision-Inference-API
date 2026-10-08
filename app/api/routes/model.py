from __future__ import annotations

import time
from typing import Annotated

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
    stage: str | None
    supported_languages: dict[str, str] | None


@router.get("/model", response_model=ModelView)
async def model(
    principal: ReadPrincipal,
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[ApiSettings, Depends(get_settings)],
) -> ModelView:
    now = time.time()
    beats = await read_heartbeats(redis)
    live = fresh(beats, now=now, stale_after=settings.heartbeat_stale_after)
    info = next((b.model for b in live if b.state is WorkerState.READY and b.model), {})
    return ModelView(
        state=model_state(beats, now=now, stale_after=settings.heartbeat_stale_after),
        stage=info.get("stage"),
        supported_languages=info.get("supported_languages"),
    )
