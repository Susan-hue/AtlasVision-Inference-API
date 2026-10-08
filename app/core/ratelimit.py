from __future__ import annotations

import math
import time
from typing import NamedTuple

from redis.asyncio import Redis

WINDOW_SECONDS = 60


class Limit(NamedTuple):
    scope: str
    per_minute: int


async def hit(
    redis: Redis, bucket: str, limits: list[Limit], now: float | None = None
) -> int | None:
    """Count one request against every limit; return Retry-After seconds if any is exceeded.

    Fixed one-minute windows: simple and cheap, at the cost of allowing up to twice the
    limit across a window boundary.
    """
    now = time.time() if now is None else now
    window = int(now // WINDOW_SECONDS)
    async with redis.pipeline(transaction=True) as pipe:
        for limit in limits:
            key = f"atlas:rl:{bucket}:{limit.scope}:{window}"
            pipe.incr(key)
            pipe.expire(key, WINDOW_SECONDS + 1)
        results = await pipe.execute()
    counts = results[::2]
    if all(count <= limit.per_minute for count, limit in zip(counts, limits, strict=True)):
        return None
    return max(1, math.ceil((window + 1) * WINDOW_SECONDS - now))
