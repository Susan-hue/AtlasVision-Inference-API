from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from redis.asyncio import Redis
from testatlasvision import Session

from app.core.config import SharedSettings
from app.core.schemas import Owner, TurnRecord

LOCK_SLACK_SECONDS = 60
SESSION_ID = re.compile(r"[0-9a-f]{32}")

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


def is_session_id(value: str) -> bool:
    return SESSION_ID.fullmatch(value) is not None


def session_key(session_id: str) -> str:
    return f"atlas:session:{session_id}"


def lock_key(session_id: str) -> str:
    return f"atlas:session:{session_id}:inflight"


def queued_lock_ttl(settings: SharedSettings) -> int:
    return settings.image_ttl + settings.job_timeout + LOCK_SLACK_SECONDS


def running_lock_ttl(settings: SharedSettings) -> int:
    return settings.job_timeout + LOCK_SLACK_SECONDS


# Named Conversation, not Session, to avoid clashing with the SDK's Session it wraps.
@dataclass
class Conversation:
    """The SDK's English Session plus, per turn, what the user actually asked and saw.

    Stored as one JSON document: Session.to_dict() with an extra "turns" list kept
    index-aligned with "history". Session.from_dict ignores the extra key.
    """

    session: Session = field(default_factory=Session)
    turns: list[TurnRecord] = field(default_factory=list)
    owner: Owner | None = None

    @property
    def session_id(self) -> str:
        return self.session.session_id

    def add_turn(self, turn: TurnRecord) -> None:
        self.turns.append(turn)

    def trim(self, max_turns: int) -> None:
        del self.session.history[:-max_turns]
        del self.turns[:-max_turns]

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.session.to_dict(),
            "turns": [t.model_dump() for t in self.turns],
            "owner": self.owner.model_dump() if self.owner is not None else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Conversation:
        session = Session.from_dict(data)
        turns = [TurnRecord.model_validate(t) for t in data.get("turns", [])]
        history = session.history
        if len(turns) > len(history):
            turns = turns[len(turns) - len(history) :]
        # History without per-turn details (should not happen) falls back to English.
        missing = [
            TurnRecord(question=t.question, question_lang="en", lang="en", answer=t.answer)
            for t in history[: len(history) - len(turns)]
        ]
        owner = Owner.model_validate(data["owner"]) if data.get("owner") else None
        return cls(session=session, turns=missing + turns, owner=owner)


async def create_session(redis: Redis, ttl: int, owner: Owner) -> Conversation:
    conversation = Conversation(owner=owner)
    await redis.set(
        session_key(conversation.session_id), json.dumps(conversation.to_dict()), ex=ttl
    )
    return conversation


async def load_session(redis: Redis, session_id: str) -> Conversation | None:
    raw = await redis.get(session_key(session_id))
    return None if raw is None else Conversation.from_dict(json.loads(raw))


async def save_session(redis: Redis, conversation: Conversation, ttl: int) -> bool:
    """Write back only if the session still exists, so a deleted session stays deleted."""
    payload = json.dumps(conversation.to_dict())
    return bool(await redis.set(session_key(conversation.session_id), payload, ex=ttl, xx=True))


async def touch_session(redis: Redis, session_id: str, ttl: int) -> bool:
    return bool(await redis.expire(session_key(session_id), ttl))


async def delete_session(redis: Redis, session_id: str) -> bool:
    return bool(await redis.delete(session_key(session_id), lock_key(session_id)))


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
