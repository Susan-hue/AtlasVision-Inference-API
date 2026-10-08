from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from redis.asyncio import Redis
from testatlasvision import GenerationConfig

from app.core.config import WorkerSettings
from app.core.jobs import delete_image, load_image, load_job, running_ttl, save_job, utcnow
from app.core.schemas import ChatOutput, JobError, JobRecord, JobStatus
from app.worker.model import ChatModel

logger = logging.getLogger(__name__)


class ImageExpired(Exception):
    pass


def job_error(exc: BaseException) -> JobError:
    # Phase 3 replaces this with the full SDK error mapping.
    if isinstance(exc, ImageExpired):
        return JobError(
            code="image_expired", message="The uploaded image expired before the job ran."
        )
    return JobError(code="internal_error", message="The job failed.")


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
        record = await _chat(redis, model, record)
    except asyncio.CancelledError:
        # arq cancels the coroutine on timeout or shutdown. The generation thread itself
        # cannot be cancelled and finishes in the background.
        error = JobError(code="job_interrupted", message="The job was interrupted.")
        await _finish(redis, settings, _failed(record, error), started)
        raise
    finally:
        await delete_image(redis, job_id)
    await _finish(redis, settings, record, started)


async def _chat(redis: Redis, model: ChatModel, record: JobRecord) -> JobRecord:
    params = record.params
    try:
        image = await load_image(redis, record.job_id)
        if image is None:
            raise ImageExpired
        result = await asyncio.to_thread(
            model.chat,
            image,
            params.question,
            lang=params.lang,
            translate_question=params.question_lang != "en",
            config=GenerationConfig(max_new_tokens=params.max_new_tokens),
        )
    except Exception as exc:
        logger.warning("Job %s failed: %s", record.job_id, type(exc).__name__, exc_info=True)
        return _failed(record, job_error(exc))
    output = ChatOutput(
        answer=result.answer,
        english_answer=result.english_answer,
        english_question=result.english_question,
        language=result.language,
    )
    return record.model_copy(update={"status": JobStatus.DONE, "result": output})


def _failed(record: JobRecord, error: JobError) -> JobRecord:
    return record.model_copy(update={"status": JobStatus.FAILED, "error": error})


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
