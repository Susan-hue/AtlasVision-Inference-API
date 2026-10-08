from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from redis.asyncio import Redis

from app.api.dependencies import get_redis, get_settings
from app.api.errors import ApiError
from app.core.config import ApiSettings
from app.core.schemas import TurnView
from app.core.sessions import (
    create_session,
    delete_session,
    load_session,
    lock_holder,
    touch_session,
)

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])

SESSION_ID = re.compile(r"[0-9a-f]{32}")

RedisDep = Annotated[Redis, Depends(get_redis)]
SettingsDep = Annotated[ApiSettings, Depends(get_settings)]


class SessionCreated(BaseModel):
    session_id: str


class SessionView(BaseModel):
    session_id: str
    history: list[TurnView]
    active_job_id: str | None


def valid_session_id(session_id: str) -> str:
    if not SESSION_ID.fullmatch(session_id):
        raise ApiError.from_code("session_not_found")
    return session_id


@router.post("", status_code=201, response_model=SessionCreated)
async def create(redis: RedisDep, settings: SettingsDep, response: Response) -> SessionCreated:
    session = await create_session(redis, settings.session_ttl)
    response.headers["Location"] = f"/v1/sessions/{session.session_id}"
    return SessionCreated(session_id=session.session_id)


@router.get("/{session_id}", response_model=SessionView)
async def get(session_id: str, redis: RedisDep, settings: SettingsDep) -> SessionView:
    conversation = await load_session(redis, valid_session_id(session_id))
    if conversation is None:
        raise ApiError.from_code("session_not_found")
    await touch_session(redis, session_id, settings.session_ttl)
    history = [
        TurnView(
            english_question=english.question, english_answer=english.answer, **turn.model_dump()
        )
        for english, turn in zip(conversation.session.history, conversation.turns, strict=True)
    ]
    return SessionView(
        session_id=session_id,
        history=history,
        active_job_id=await lock_holder(redis, session_id),
    )


@router.delete("/{session_id}", status_code=204)
async def delete(session_id: str, redis: RedisDep) -> Response:
    if not await delete_session(redis, valid_session_id(session_id)):
        raise ApiError.from_code("session_not_found")
    return Response(status_code=204)
