from __future__ import annotations

import json

from redis.asyncio import Redis
from testatlasvision import Session

from app.core.config import SharedSettings

LOCK_SLACK_SECONDS = 60

# Lock values are job ids. Each script is one atomic check-then-act on the lock key.
_ACQUIRE = """
if redis.call('SET', KEYS[1], ARGV[1], 'NX', 'EX', ARGV[2]) then return false end
return redis.call('GET', KEYS[1])
"""
_REPLACE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
  return 1
end
return 0
"""
_REFRESH = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('EXPIRE', KEYS[1], ARGV[2]) end
return 0
"""
_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""


def session_key(session_id: str) -> str:
    return f"atlas:session:{session_id}"


def lock_key(session_id: str) -> str:
    return f"atlas:session:{session_id}:inflight"


def queued_lock_ttl(settings: SharedSettings) -> int:
    return settings.image_ttl + settings.job_timeout + LOCK_SLACK_SECONDS


def running_lock_ttl(settings: SharedSettings) -> int:
    return settings.job_timeout + LOCK_SLACK_SECONDS


async def create_session(redis: Redis, ttl: int) -> Session:
    session = Session()
    await redis.set(session_key(session.session_id), json.dumps(session.to_dict()), ex=ttl)
    return session


async def load_session(redis: Redis, session_id: str) -> Session | None:
    raw = await redis.get(session_key(session_id))
    return None if raw is None else Session.from_dict(json.loads(raw))


async def save_session(redis: Redis, session: Session, ttl: int) -> bool:
    """Write back only if the session still exists, so a deleted session stays deleted."""
    payload = json.dumps(session.to_dict())
    return bool(await redis.set(session_key(session.session_id), payload, ex=ttl, xx=True))


async def touch_session(redis: Redis, session_id: str, ttl: int) -> bool:
    return bool(await redis.expire(session_key(session_id), ttl))


async def delete_session(redis: Redis, session_id: str) -> bool:
    return bool(await redis.delete(session_key(session_id), lock_key(session_id)))


def trim_history(session: Session, max_turns: int) -> None:
    del session.history[:-max_turns]


async def acquire_lock(redis: Redis, session_id: str, job_id: str, ttl: int) -> str | None:
    """Take the session's lock for `job_id`. Returns the holder's job id if already taken."""
    holder = await redis.register_script(_ACQUIRE)(keys=[lock_key(session_id)], args=[job_id, ttl])
    return holder.decode() if holder else None


async def replace_lock(redis: Redis, session_id: str, old: str, new: str, ttl: int) -> bool:
    script = redis.register_script(_REPLACE)
    return bool(await script(keys=[lock_key(session_id)], args=[old, new, ttl]))


async def refresh_lock(redis: Redis, session_id: str, job_id: str, ttl: int) -> bool:
    script = redis.register_script(_REFRESH)
    return bool(await script(keys=[lock_key(session_id)], args=[job_id, ttl]))


async def release_lock(redis: Redis, session_id: str, job_id: str) -> None:
    await redis.register_script(_RELEASE)(keys=[lock_key(session_id)], args=[job_id])


async def lock_holder(redis: Redis, session_id: str) -> str | None:
    holder = await redis.get(lock_key(session_id))
    return holder.decode() if holder else None
