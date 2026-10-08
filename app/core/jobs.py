from __future__ import annotations

from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis

from app.core.config import SharedSettings
from app.core.schemas import JobError, JobRecord, JobStatus

# Slack for clock skew between API and worker hosts and for Redis round trips.
GRACE = timedelta(seconds=30)


def job_key(job_id: str) -> str:
    return f"atlas:job:{job_id}"


def image_key(job_id: str) -> str:
    return f"atlas:job:{job_id}:image"


def utcnow() -> datetime:
    return datetime.now(UTC)


def queued_ttl(settings: SharedSettings) -> int:
    return settings.image_ttl + settings.job_timeout + settings.result_ttl


def running_ttl(settings: SharedSettings) -> int:
    return settings.job_timeout + settings.result_ttl


async def create_job(
    redis: Redis, record: JobRecord, image: bytes, settings: SharedSettings
) -> None:
    async with redis.pipeline(transaction=True) as pipe:
        pipe.set(image_key(record.job_id), image, ex=settings.image_ttl)
        pipe.set(job_key(record.job_id), record.model_dump_json(), ex=queued_ttl(settings))
        await pipe.execute()


async def delete_job(redis: Redis, job_id: str) -> None:
    await redis.delete(job_key(job_id), image_key(job_id))


async def save_job(redis: Redis, record: JobRecord, *, ttl: int) -> None:
    await redis.set(job_key(record.job_id), record.model_dump_json(), ex=ttl)


async def load_job(redis: Redis, job_id: str) -> JobRecord | None:
    raw = await redis.get(job_key(job_id))
    return None if raw is None else JobRecord.model_validate_json(raw)


async def load_image(redis: Redis, job_id: str) -> bytes | None:
    return await redis.get(image_key(job_id))


async def delete_image(redis: Redis, job_id: str) -> None:
    await redis.delete(image_key(job_id))


def as_seen_now(record: JobRecord, settings: SharedSettings, now: datetime) -> JobRecord:
    """Report jobs that can no longer finish as failed.

    arq drops expired jobs without calling us, and a crashed worker never writes a final
    status, so those records would otherwise stay queued/running until their TTL.
    """
    if record.status is JobStatus.QUEUED:
        deadline = record.created_at + timedelta(seconds=settings.image_ttl) + GRACE
        if now > deadline:
            return _failed(record, "job_expired", "The job waited too long in the queue.")
    elif record.status is JobStatus.RUNNING and record.started_at is not None:
        deadline = record.started_at + timedelta(seconds=settings.job_timeout) + GRACE
        if now > deadline:
            return _failed(record, "worker_lost", "The worker stopped before finishing the job.")
    return record


def _failed(record: JobRecord, code: str, message: str) -> JobRecord:
    return record.model_copy(
        update={"status": JobStatus.FAILED, "error": JobError(code=code, message=message)}
    )
