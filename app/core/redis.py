from __future__ import annotations

import asyncio
import logging

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

PING_TIMEOUT_SECONDS = 1.0


def create_redis(url: str) -> Redis:
    return Redis.from_url(
        url,
        decode_responses=False,
        socket_connect_timeout=2,
        socket_timeout=5,
        health_check_interval=30,
    )


async def ping(redis: Redis) -> bool:
    try:
        async with asyncio.timeout(PING_TIMEOUT_SECONDS):
            return bool(await redis.ping())
    except (RedisError, OSError, TimeoutError) as exc:
        # Type only: the message can contain the Redis URL, including its password.
        logger.warning("Redis ping failed: %s", type(exc).__name__)
        return False
