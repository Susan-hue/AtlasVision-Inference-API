from __future__ import annotations

import asyncio
import re
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
from app.core.config import ApiSettings
from app.core.heartbeat import accepts_jobs, model_state, read_heartbeats
from app.core.images import ImageRejected, check_image
from app.core.jobs import as_seen_now, create_job, delete_job, load_job, utcnow
from app.core.queue import CHAT_JOB, enqueue
from app.core.schemas import ChatOutput, ChatParams, JobError, JobRecord, JobStatus, Lang

router = APIRouter(prefix="/v1/jobs", tags=["jobs"])

JOB_ID = re.compile(r"[0-9a-f]{32}")

RedisDep = Annotated[Redis, Depends(get_redis)]
SettingsDep = Annotated[ApiSettings, Depends(get_settings)]


class JobAccepted(BaseModel):
    job_id: str
    status: JobStatus


class JobView(BaseModel):
    job_id: str
    status: JobStatus
    created_at: datetime
    finished_at: datetime | None
    result: ChatOutput | None
    error: JobError | None


@router.post("/chat", status_code=202, response_model=JobAccepted)
async def submit_chat(
    response: Response,
    redis: RedisDep,
    settings: SettingsDep,
    image: Annotated[UploadFile, File()],
    question: Annotated[str, Form()],
    lang: Annotated[str, Form()],
    question_lang: Annotated[str, Form()],
) -> JobAccepted:
    params = ChatParams(
        question=_question(question, settings),
        lang=_language(lang, "lang"),
        question_lang=_language(question_lang, "question_lang"),
        max_new_tokens=settings.max_new_tokens,
    )
    if params.question_lang not in ("en", params.lang):
        raise ApiError(422, "invalid_question_lang", 'question_lang must be "en" or equal to lang.')
    await _require_worker(redis, settings)
    data = await _read_image(image, settings)

    record = JobRecord(
        job_id=uuid.uuid4().hex, status=JobStatus.QUEUED, created_at=utcnow(), params=params
    )
    await create_job(redis, record, data, settings)
    try:
        await enqueue(redis, CHAT_JOB, record.job_id, expires=settings.image_ttl)
    except BaseException:
        await delete_job(redis, record.job_id)
        raise

    response.headers["Location"] = f"/v1/jobs/{record.job_id}"
    return JobAccepted(job_id=record.job_id, status=record.status)


@router.get("/{job_id}", response_model=JobView)
async def get_job(job_id: str, redis: RedisDep, settings: SettingsDep) -> JobView:
    record = await load_job(redis, job_id) if JOB_ID.fullmatch(job_id) else None
    if record is None:
        raise ApiError(404, "job_not_found", "Job not found or expired.")
    record = as_seen_now(record, settings, utcnow())
    return JobView.model_validate(record, from_attributes=True)


def _question(question: str, settings: ApiSettings) -> str:
    question = question.strip()
    if not question:
        raise ApiError(422, "invalid_question", "question must not be empty.")
    if len(question) > settings.max_question_chars:
        raise ApiError(
            422,
            "question_too_long",
            f"question must be at most {settings.max_question_chars} characters.",
        )
    return question


def _language(value: str, field: str) -> Lang:
    try:
        return normalize_language(value)  # type: ignore[return-value]
    except UnsupportedLanguageError:
        raise ApiError(
            422, "unsupported_language", f"{field} must be one of en, ig, yo, ha."
        ) from None


async def _require_worker(redis: Redis, settings: ApiSettings) -> None:
    beats = await read_heartbeats(redis)
    state = model_state(beats, now=time.time(), stale_after=settings.heartbeat_stale_after)
    if not accepts_jobs(state):
        raise ApiError(
            503,
            "model_unavailable",
            "The model is not available. Try again later.",
            headers={"Retry-After": "30"},
        )


async def _read_image(image: UploadFile, settings: ApiSettings) -> bytes:
    data = await image.read(settings.max_image_bytes + 1)
    if len(data) > settings.max_image_bytes:
        raise ApiError(
            413, "image_too_large", f"Image must be at most {settings.max_image_mb:g} MB."
        )
    try:
        await asyncio.to_thread(check_image, data, image.content_type)
    except ImageRejected as exc:
        status = 415 if exc.code == "unsupported_media_type" else 422
        raise ApiError(status, exc.code, exc.message) from None
    return data
