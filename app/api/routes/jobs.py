from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile
from pydantic import BaseModel
from redis.asyncio import Redis
from testatlasvision import UnsupportedLanguageError, normalize_language

from app.api.dependencies import get_redis, get_settings
from app.api.errors import ApiError
from app.api.limits import ReadPrincipal, SubmitPrincipal
from app.core.config import ApiSettings
from app.core.heartbeat import accepts_jobs, model_state, read_heartbeats
from app.core.images import ImageRejected, check_image
from app.core.jobs import as_seen_now, create_job, delete_job, is_job_id, load_job, utcnow
from app.core.queue import CHAT_JOB, enqueue, queue_depth
from app.core.schemas import ChatOutput, ChatParams, JobError, JobRecord, JobStatus, Lang, Owner
from app.core.sessions import (
    acquire_lock,
    is_session_id,
    load_session,
    queued_lock_ttl,
    release_lock,
    replace_lock,
    touch_session,
)

router = APIRouter(prefix="/v1/jobs", tags=["jobs"])

RedisDep = Annotated[Redis, Depends(get_redis)]
SettingsDep = Annotated[ApiSettings, Depends(get_settings)]


class JobAccepted(BaseModel):
    job_id: str
    status: JobStatus


class JobView(BaseModel):
    job_id: str
    status: JobStatus
    session_id: str | None
    created_at: datetime
    finished_at: datetime | None
    result: ChatOutput | None
    error: JobError | None


@router.post("/chat", status_code=202, response_model=JobAccepted)
async def submit_chat(
    principal: SubmitPrincipal,
    response: Response,
    redis: RedisDep,
    settings: SettingsDep,
    image: Annotated[UploadFile, File()],
    lang: Annotated[str, Form()],
    question_lang: Annotated[str, Form()],
    # A default, so an empty or missing question gets invalid_question, not invalid_request.
    question: Annotated[str, Form()] = "",
    session_id: Annotated[str | None, Form()] = None,
) -> JobAccepted:
    params = ChatParams(
        question=_question(question, settings),
        lang=_language(lang, "lang"),
        question_lang=_language(question_lang, "question_lang"),
        max_new_tokens=settings.max_new_tokens,
    )
    if params.question_lang not in ("en", params.lang):
        raise ApiError("invalid_question_lang")
    session_id = (session_id or "").strip() or None
    if session_id is not None and not is_session_id(session_id):
        raise ApiError("session_not_found")
    await _require_worker(redis, settings)
    if await queue_depth(redis) >= settings.max_queue_depth:
        raise ApiError("queue_full", headers={"Retry-After": str(settings.retry_after_queue_full)})
    data = await _read_image(image, settings)

    record = JobRecord(
        job_id=uuid.uuid4().hex,
        status=JobStatus.QUEUED,
        owner=principal.owner,
        session_id=session_id,
        created_at=utcnow(),
        params=params,
    )
    if session_id is not None:
        await _lock_session(redis, settings, principal.owner, session_id, record.job_id)
    try:
        await create_job(redis, record, data, settings)
        await enqueue(redis, CHAT_JOB, record.job_id, expires=settings.image_ttl)
    except BaseException:
        await delete_job(redis, record.job_id)
        if session_id is not None:
            await release_lock(redis, session_id, record.job_id)
        raise

    response.headers["Location"] = f"/v1/jobs/{record.job_id}"
    return JobAccepted(job_id=record.job_id, status=record.status)


@router.get("/{job_id}", response_model=JobView)
async def get_job(
    job_id: str, principal: ReadPrincipal, redis: RedisDep, settings: SettingsDep
) -> JobView:
    record = await load_job(redis, job_id) if is_job_id(job_id) else None
    # Someone else's job is reported exactly like a missing one.
    if record is None or record.owner != principal.owner:
        raise ApiError("job_not_found")
    record = as_seen_now(record, settings, utcnow())
    return JobView.model_validate(record, from_attributes=True)


def _question(question: str, settings: ApiSettings) -> str:
    question = question.strip()
    if not question:
        raise ApiError("invalid_question")
    if len(question) > settings.max_question_chars:
        raise ApiError("question_too_long", extra={"max_length": settings.max_question_chars})
    return question


def _language(value: str, field: str) -> Lang:
    try:
        return normalize_language(value)  # type: ignore[return-value]
    except UnsupportedLanguageError:
        raise ApiError("unsupported_language", extra={"field": field}) from None


async def _lock_session(
    redis: Redis, settings: ApiSettings, owner: Owner, session_id: str, job_id: str
) -> None:
    conversation = await load_session(redis, session_id)
    if conversation is None or conversation.owner != owner:
        raise ApiError("session_not_found")
    await touch_session(redis, session_id, settings.session_ttl)
    ttl = queued_lock_ttl(settings)
    holder = await acquire_lock(redis, session_id, job_id, ttl)
    if holder is None:
        return
    # A holder that can no longer finish (expired in the queue, lost worker) must not
    # block the conversation until its lock times out.
    if await _cannot_finish(redis, settings, holder) and await replace_lock(
        redis, session_id, holder, job_id, ttl
    ):
        return
    raise ApiError("session_busy", extra={"job_id": holder})


async def _cannot_finish(redis: Redis, settings: ApiSettings, job_id: str) -> bool:
    record = await load_job(redis, job_id)
    if record is None:
        return True
    return as_seen_now(record, settings, utcnow()).status in (JobStatus.DONE, JobStatus.FAILED)


async def _require_worker(redis: Redis, settings: ApiSettings) -> None:
    beats = await read_heartbeats(redis)
    state = model_state(beats, now=time.time(), stale_after=settings.heartbeat_stale_after)
    if not accepts_jobs(state):
        retry_after = str(settings.retry_after_model_unavailable)
        raise ApiError("model_unavailable", headers={"Retry-After": retry_after})


async def _read_image(image: UploadFile, settings: ApiSettings) -> bytes:
    data = await image.read(settings.max_image_bytes + 1)
    if len(data) > settings.max_image_bytes:
        raise ApiError("request_too_large")
    try:
        await asyncio.to_thread(check_image, data, image.content_type)
    except ImageRejected as exc:
        raise ApiError(exc.code) from None
    return data
