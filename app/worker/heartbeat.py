from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import time
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.heartbeat import WORKERS_KEY, read_heartbeats
from app.core.schemas import Heartbeat, WorkerState

logger = logging.getLogger(__name__)

PRUNE_AFTER_SECONDS = 3600


def worker_id() -> str:
    # Stable across restarts of the same container, so a restarted worker overwrites its
    # own entry instead of leaving a dead one behind.
    return f"{socket.gethostname()}:{os.getpid()}"


class HeartbeatPublisher:
    """Publishes this worker's state on a plain asyncio task.

    Not an arq cron job: with max_jobs=1 a cron job would wait for the running generation,
    so the heartbeat would go stale exactly when the worker is busiest.
    """

    def __init__(self, redis: Redis, worker_id: str, interval: float) -> None:
        self.worker_id = worker_id
        self.state = WorkerState.LOADING
        self.model_info: dict[str, Any] | None = None
        self.error_code: str | None = None
        self._redis = redis
        self._interval = interval
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def set_state(
        self,
        state: WorkerState,
        *,
        model_info: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> None:
        self.state, self.model_info, self.error_code = state, model_info, error_code
        await self.publish()

    async def publish(self) -> None:
        beat = Heartbeat(
            worker_id=self.worker_id,
            state=self.state,
            updated_at=time.time(),
            model=self.model_info,
            error_code=self.error_code,
        )
        await self._redis.hset(WORKERS_KEY, self.worker_id, beat.model_dump_json())

    async def stop(self, *, remove: bool) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if remove:
            await self._redis.hdel(WORKERS_KEY, self.worker_id)

    async def _run(self) -> None:
        while True:
            try:
                await self.publish()
            except RedisError as exc:
                logger.warning("Heartbeat publish failed: %s", type(exc).__name__)
            await asyncio.sleep(self._interval)


async def prune_dead_workers(redis: Redis) -> None:
    cutoff = time.time() - PRUNE_AFTER_SECONDS
    dead = [b.worker_id for b in await read_heartbeats(redis) if b.updated_at < cutoff]
    if dead:
        await redis.hdel(WORKERS_KEY, *dead)
