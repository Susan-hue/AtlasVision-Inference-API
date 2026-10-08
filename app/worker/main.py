from __future__ import annotations

import asyncio
import logging
import signal
from typing import Any

from arq import func
from arq.connections import RedisSettings

from app.core.config import WorkerSettings
from app.core.queue import CHAT_JOB, deserialize, serialize
from app.core.redis import create_redis
from app.core.schemas import WorkerState
from app.worker.heartbeat import HeartbeatPublisher, prune_dead_workers, worker_id
from app.worker.jobs import run_chat_job
from app.worker.model import load_error_code, load_model

logger = logging.getLogger(__name__)


class ModelStartupError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def startup(ctx: dict[str, Any]) -> None:
    settings: WorkerSettings = ctx["settings"]
    heartbeat = HeartbeatPublisher(ctx["redis"], worker_id(), settings.heartbeat_interval)
    ctx["heartbeat"] = heartbeat
    await prune_dead_workers(ctx["redis"])
    heartbeat.start()

    logger.info("Loading model (fake=%s)", settings.use_fake_model)
    try:
        model = await asyncio.to_thread(load_model, settings)
    except Exception as exc:
        code = load_error_code(exc)
        logger.error("Model failed to load (%s): %s: %s", code, type(exc).__name__, exc)
        await heartbeat.set_state(WorkerState.FAILED, error_code=code)
        raise ModelStartupError(code) from exc

    ctx["model"] = model
    info = model.info().to_dict()
    await heartbeat.set_state(WorkerState.READY, model_info=info)
    logger.info("Model ready: %s", info)


async def shutdown(ctx: dict[str, Any]) -> None:
    heartbeat: HeartbeatPublisher | None = ctx.get("heartbeat")
    if heartbeat is not None:
        # Keep a "failed" entry so the API can report why until it goes stale.
        await heartbeat.stop(remove=heartbeat.state is not WorkerState.FAILED)
    model = ctx.get("model")
    if model is not None:
        await asyncio.to_thread(model.close)


async def park(settings: WorkerSettings, code: str, stop: asyncio.Event | None = None) -> None:
    """Keep reporting a permanent load failure until stopped, instead of exiting.

    Exiting would make Docker restart the worker over and over, re-checking a token that is
    still wrong. Staying up keeps the "failed" heartbeat fresh, so the API reports why it
    rejects jobs, until an operator fixes the token and recreates the container.
    """
    if stop is None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
    redis = create_redis(settings.redis_url)
    heartbeat = HeartbeatPublisher(redis, worker_id(), settings.heartbeat_interval)
    heartbeat.state, heartbeat.error_code = WorkerState.FAILED, code
    heartbeat.start()
    try:
        await stop.wait()
    finally:
        await heartbeat.stop(remove=False)
        await redis.aclose()


def worker_options(settings: WorkerSettings) -> dict[str, Any]:
    return {
        "functions": [func(run_chat_job, name=CHAT_JOB, max_tries=1, timeout=settings.job_timeout)],
        "on_startup": startup,
        "on_shutdown": shutdown,
        "redis_settings": RedisSettings.from_dsn(settings.redis_url),
        "ctx": {"settings": settings},
        "max_jobs": 1,
        "max_tries": 1,
        "job_timeout": settings.job_timeout,
        "keep_result": 0,
        "job_serializer": serialize,
        "job_deserializer": deserialize,
    }
