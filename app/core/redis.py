"""Redis connection helpers shared by the API and the worker."""

from __future__ import annotations

import asyncio
import logging

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

PING_TIMEOUT_SECONDS = 1.0


def create_redis(url: str) -> Redis:
    """Build a pooled async client.

    Responses stay as bytes (`decode_responses=False`) because some values are raw image
    bytes; JSON values are decoded explicitly where they are read. Short timeouts make a
    dead Redis surface as an error quickly instead of hanging requests.
    """
    return Redis.from_url(
        url,
        decode_responses=False,
        socket_connect_timeout=2,
        socket_timeout=5,
        health_check_interval=30,
    )


async def ping(redis: Redis) -> bool:
    """True if Redis answers PING within PING_TIMEOUT_SECONDS."""
    try:
        async with asyncio.timeout(PING_TIMEOUT_SECONDS):
            return bool(await redis.ping())
    except (RedisError, OSError, TimeoutError) as exc:
        # Log the error type only: the message can include the URL, and the URL can
        # include a password.
        logger.warning("Redis ping failed: %s", type(exc).__name__)
        return False
