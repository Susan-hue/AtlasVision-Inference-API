from __future__ import annotations

import asyncio
import io
import os
import time
from collections.abc import Iterator
from typing import Any

import pytest
import redis
from arq import Worker
from fastapi.testclient import TestClient
from PIL import Image

from app.api.main import create_app
from app.core.config import ApiSettings, WorkerSettings
from app.core.heartbeat import WORKERS_KEY
from app.core.schemas import Heartbeat, WorkerState
from app.worker.main import worker_options

TEST_REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/15")


def make_api_settings(**overrides: Any) -> ApiSettings:
    return ApiSettings(_env_file=None, **{"redis_url": TEST_REDIS_URL, **overrides})


def make_worker_settings(**overrides: Any) -> WorkerSettings:
    defaults: dict[str, Any] = {
        "redis_url": TEST_REDIS_URL,
        "use_fake_model": True,
        "fake_load_seconds": 0,
        "fake_generation_seconds": 0,
        "heartbeat_interval": 1,
        "heartbeat_stale_after": 5,
    }
    return WorkerSettings(_env_file=None, **{**defaults, **overrides})


def image_bytes(width: int = 32, height: int = 24, fmt: str = "PNG") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buffer, format=fmt)
    return buffer.getvalue()


def run_worker(settings: WorkerSettings) -> None:
    """Run a real arq worker until the queue is empty."""

    async def _run() -> None:
        worker = Worker(
            **worker_options(settings), burst=True, poll_delay=0.01, handle_signals=False
        )
        try:
            await worker.async_run()
        finally:
            await worker.close()

    asyncio.run(_run())


@pytest.fixture
def sync_redis() -> Iterator[redis.Redis]:
    client = redis.Redis.from_url(TEST_REDIS_URL)
    client.flushdb()
    yield client
    client.close()


@pytest.fixture
def publish_heartbeat(sync_redis: redis.Redis):
    def publish(state: WorkerState = WorkerState.READY, *, age: float = 0.0) -> None:
        beat = Heartbeat(worker_id="test-worker", state=state, updated_at=time.time() - age)
        sync_redis.hset(WORKERS_KEY, beat.worker_id, beat.model_dump_json())

    return publish


@pytest.fixture
def client(sync_redis: redis.Redis) -> Iterator[TestClient]:
    with TestClient(create_app(make_api_settings())) as test_client:
        yield test_client


def submit_chat(
    client: TestClient,
    *,
    image: bytes | None = None,
    content_type: str = "image/png",
    **fields: str,
) -> Any:
    data = {"question": "What is this?", "lang": "yo", "question_lang": "en", **fields}
    files = {"image": ("photo", image if image is not None else image_bytes(), content_type)}
    return client.post("/v1/jobs/chat", data=data, files=files)
