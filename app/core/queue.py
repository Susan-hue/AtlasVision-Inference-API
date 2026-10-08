"""The only module (besides the worker entry point) that knows the queue is arq."""

from __future__ import annotations

import json
from typing import Any

from arq.connections import ArqRedis
from arq.constants import default_queue_name
from redis.asyncio import Redis

CHAT_JOB = "run_chat_job"


# arq pickles job payloads by default; JSON means data read back from Redis can't run code.
def serialize(data: dict[str, Any]) -> bytes:
    return json.dumps(data).encode()


def deserialize(data: bytes) -> dict[str, Any]:
    return json.loads(data)


def _arq(redis: Redis) -> ArqRedis:
    return ArqRedis(
        pool_or_conn=redis.connection_pool,
        job_serializer=serialize,
        job_deserializer=deserialize,
    )


async def enqueue(redis: Redis, function: str, job_id: str, *, expires: int) -> None:
    job = await _arq(redis).enqueue_job(function, job_id, _job_id=job_id, _expires=expires)
    if job is None:
        raise RuntimeError(f"Job {job_id} is already queued")


async def queue_depth(redis: Redis) -> int:
    return await redis.zcard(default_queue_name)
