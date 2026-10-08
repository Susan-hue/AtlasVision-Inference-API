from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from redis.asyncio import Redis
from testatlasvision import GenerationConfig, Session

from app.core.config import WorkerSettings
from app.core.errors import code_for, job_error
from app.core.jobs import delete_image, load_image, load_job, running_ttl, save_job, utcnow
from app.core.schemas import ChatOutput, JobRecord, JobStatus
from app.core.sessions import (
    load_session,
    refresh_lock,
    release_lock,
    running_lock_ttl,
    save_session,
    trim_history,
)
from app.worker.model import ChatModel

logger = logging.getLogger(__name__)


class JobFailure(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def run_chat_job(ctx: dict[str, Any], job_id: str) -> None:
    redis: Redis = ctx["redis"]
    settings: WorkerSettings = ctx["settings"]
    model: ChatModel = ctx["model"]

    record = await load_job(redis, job_id)
    if record is None:
        logger.warning("Job %s has no record; skipping", job_id)
        await delete_image(redis, job_id)
        return

    record = record.model_copy(update={"status": JobStatus.RUNNING, "started_at": utcnow()})
    await save_job(redis, record, ttl=running_ttl(settings))
    started = time.monotonic()
    try:
        record = await _chat(redis, settings, model, record)
    except asyncio.CancelledError:
        # arq cancels the coroutine on timeout or shutdown. The generation thread itself
        # cannot be cancelled and finishes in the background.
        record = _failed(record, "job_interrupted")
        raise
    finally:
        await delete_image(redis, job_id)
        # Release before publishing the result, so a client that sees "done" can ask next.
        if record.session_id is not None:
            await release_lock(redis, record.session_id, job_id)
        await _finish(redis, settings, record, started)


async def _chat(
    redis: Redis, settings: WorkerSettings, model: ChatModel, record: JobRecord
) -> JobRecord:
    params = record.params
    try:
        session = await _open_session(redis, settings, record)
        image = await load_image(redis, record.job_id)
        if image is None:
            raise JobFailure("image_expired")
        result = await asyncio.to_thread(
            model.chat,
            image,
            params.question,
            lang=params.lang,
            session=session,
            translate_question=params.question_lang != "en",
            config=GenerationConfig(max_new_tokens=params.max_new_tokens),
        )
        if session is not None:
            trim_history(session, settings.max_session_turns)
            await save_session(redis, session, settings.session_ttl)
    except Exception as exc:
        code = exc.code if isinstance(exc, JobFailure) else code_for(exc)
        logger.warning(
            "Job %s failed (%s): %s", record.job_id, code, type(exc).__name__, exc_info=True
        )
        return _failed(record, code)
    output = ChatOutput(
        answer=result.answer,
        english_answer=result.english_answer,
        english_question=result.english_question,
        language=result.language,
    )
    return record.model_copy(update={"status": JobStatus.DONE, "result": output})


async def _open_session(
    redis: Redis, settings: WorkerSettings, record: JobRecord
) -> Session | None:
    if record.session_id is None:
        return None
    session = await load_session(redis, record.session_id)
    if session is None:
        raise JobFailure("session_not_found")
    if not await refresh_lock(redis, record.session_id, record.job_id, running_lock_ttl(settings)):
        raise JobFailure("session_busy")
    trim_history(session, settings.max_session_turns)
    return session


def _failed(record: JobRecord, code: str) -> JobRecord:
    return record.model_copy(update={"status": JobStatus.FAILED, "error": job_error(code)})


async def _finish(
    redis: Redis, settings: WorkerSettings, record: JobRecord, started: float
) -> None:
    record = record.model_copy(update={"finished_at": utcnow()})
    await save_job(redis, record, ttl=settings.result_ttl)
    logger.info(
        "Job %s %s in %.2fs", record.job_id, record.status.value, time.monotonic() - started
    )
    if settings.log_content and record.result is not None:
        logger.info(
            "Job %s question=%r answer=%r",
            record.job_id,
            record.params.question,
            record.result.answer,
        )
