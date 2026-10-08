"""Sliding window counter rate limiting.

Each limit keeps one counter per fixed minute. A request's usage is estimated as
    previous_minute * (fraction of the current minute still to come) + current_minute
which smooths out the burst a plain fixed window allows across a minute boundary.
"""

from __future__ import annotations

import math
import time
from typing import NamedTuple

from redis.asyncio import Redis

WINDOW_SECONDS = 60


class Limit(NamedTuple):
    scope: str
    per_minute: int


def usage(previous: int, current: int, elapsed: float) -> float:
    return previous * (1 - elapsed / WINDOW_SECONDS) + current


def seconds_until_allowed(limit: int, previous: int, current: int, elapsed: float) -> float:
    """How long until one more request fits, assuming nothing else arrives meanwhile."""
    if current + 1 <= limit:
        if previous == 0:
            return 0.0
        # Solve previous * (1 - t / W) + current + 1 <= limit for t within this window.
        fits_at = WINDOW_SECONDS * (1 - (limit - current - 1) / previous)
        return max(0.0, fits_at - elapsed)
    # Not in this window: in the next one, this window's count becomes "previous".
    fits_at = WINDOW_SECONDS * max(0.0, 1 - (limit - 1) / current)
    return (WINDOW_SECONDS - elapsed) + fits_at


async def hit(
    redis: Redis, bucket: str, limits: list[Limit], now: float | None = None
) -> int | None:
    """Count one request against every limit; return Retry-After seconds if any is exceeded."""
    now = time.time() if now is None else now
    window = int(now // WINDOW_SECONDS)
    elapsed = now - window * WINDOW_SECONDS

    # One MULTI/EXEC: every increment and read for this request happens atomically.
    async with redis.pipeline(transaction=True) as pipe:
        for limit in limits:
            current_key = f"atlas:rl:{bucket}:{limit.scope}:{window}"
            pipe.incr(current_key)
            pipe.expire(current_key, 2 * WINDOW_SECONDS)
            pipe.get(f"atlas:rl:{bucket}:{limit.scope}:{window - 1}")
        results = await pipe.execute()

    waits = []
    for i, limit in enumerate(limits):
        current, previous = int(results[3 * i]), int(results[3 * i + 2] or 0)
        if usage(previous, current, elapsed) > limit.per_minute:
            waits.append(seconds_until_allowed(limit.per_minute, previous, current, elapsed))
    if not waits:
        return None
    return max(1, math.ceil(max(waits) - 1e-9))
