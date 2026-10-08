from __future__ import annotations

import logging
from enum import StrEnum

from pydantic import ValidationError
from redis.asyncio import Redis

from app.core.schemas import Heartbeat, WorkerState

logger = logging.getLogger(__name__)

# One hash field per worker. A hash (not one key per worker) lets readers fetch every
# worker in one HGETALL instead of SCANning the keyspace.
WORKERS_KEY = "atlas:workers"


class ModelState(StrEnum):
    READY = "ready"
    LOADING = "loading"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


async def read_heartbeats(redis: Redis) -> list[Heartbeat]:
    beats = []
    for worker_id, raw in (await redis.hgetall(WORKERS_KEY)).items():
        try:
            beats.append(Heartbeat.model_validate_json(raw))
        except ValidationError:
            logger.warning("Ignoring malformed heartbeat for worker %r", worker_id)
    return beats


def fresh(beats: list[Heartbeat], *, now: float, stale_after: float) -> list[Heartbeat]:
    return [b for b in beats if now - b.updated_at <= stale_after]


def model_state(beats: list[Heartbeat], *, now: float, stale_after: float) -> ModelState:
    states = {b.state for b in fresh(beats, now=now, stale_after=stale_after)}
    if WorkerState.READY in states:
        return ModelState.READY
    if WorkerState.LOADING in states:
        return ModelState.LOADING
    if WorkerState.FAILED in states:
        return ModelState.FAILED
    return ModelState.UNAVAILABLE


def accepts_jobs(state: ModelState) -> bool:
    return state in (ModelState.READY, ModelState.LOADING)
