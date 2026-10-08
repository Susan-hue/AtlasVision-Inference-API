from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
import redis
from fastapi.testclient import TestClient

from app.core.heartbeat import WORKERS_KEY
from app.core.jobs import image_key
from app.core.redis import create_redis
from app.core.schemas import WorkerState
from app.worker import __main__ as worker_entry
from app.worker.main import ModelStartupError, park, shutdown, startup
from app.worker.model import PERMANENT_LOAD_ERRORS
from tests.conftest import make_worker_settings, run_worker, submit_chat


@pytest.fixture
def ready(publish_heartbeat) -> None:
    publish_heartbeat(WorkerState.READY)


def _poll(client: TestClient, job_id: str) -> dict[str, Any]:
    return client.get(f"/v1/jobs/{job_id}").json()


def test_chat_job_end_to_end(client: TestClient, sync_redis: redis.Redis, ready: None) -> None:
    job_id = submit_chat(client, question="What is this?", lang="yo", question_lang="en").json()[
        "job_id"
    ]

    run_worker(make_worker_settings())

    body = _poll(client, job_id)
    assert body["status"] == "done"
    assert body["error"] is None
    assert body["finished_at"] is not None
    assert body["result"] == {
        "answer": "[en->yo] Fake answer 1 about a 32x24 image: What is this?",
        "english_answer": "Fake answer 1 about a 32x24 image: What is this?",
        "english_question": "What is this?",
        "language": "yo",
    }
    assert sync_redis.exists(image_key(job_id)) == 0


def test_non_english_question_is_translated_first(client: TestClient, ready: None) -> None:
    job_id = submit_chat(client, question="Kini eyi?", lang="yo", question_lang="yo").json()[
        "job_id"
    ]

    run_worker(make_worker_settings())

    assert _poll(client, job_id)["result"]["english_question"] == "[yo->en] Kini eyi?"


def test_sdk_error_fails_job_and_deletes_image(
    client: TestClient, sync_redis: redis.Redis, ready: None
) -> None:
    job_id = submit_chat(client, question="__raise_ImageLoadError__").json()["job_id"]

    run_worker(make_worker_settings())

    body = _poll(client, job_id)
    assert body["status"] == "failed"
    assert body["error"] == {"code": "invalid_image", "message": "The image could not be read."}
    assert sync_redis.exists(image_key(job_id)) == 0


def test_expired_image_fails_job(client: TestClient, sync_redis: redis.Redis, ready: None) -> None:
    job_id = submit_chat(client).json()["job_id"]
    sync_redis.delete(image_key(job_id))

    run_worker(make_worker_settings())

    assert _poll(client, job_id)["error"]["code"] == "image_expired"


def test_job_timeout_marks_job_interrupted(client: TestClient, ready: None) -> None:
    job_id = submit_chat(client, lang="en").json()["job_id"]

    run_worker(make_worker_settings(job_timeout=1, fake_generation_seconds=2))

    body = _poll(client, job_id)
    assert body["status"] == "failed"
    assert body["error"]["code"] == "job_interrupted"


def _heartbeat(sync_redis: redis.Redis) -> dict[str, Any]:
    entries = sync_redis.hgetall(WORKERS_KEY)
    assert len(entries) == 1
    return json.loads(next(iter(entries.values())))


async def _start_and_stop(settings, sync_redis: redis.Redis, seen: list[str]) -> None:
    ctx: dict[str, Any] = {"redis": create_redis(settings.redis_url), "settings": settings}
    try:
        load = asyncio.create_task(startup(ctx))
        await asyncio.sleep(0.3)
        seen.append(_heartbeat(sync_redis)["state"])
        try:
            await load
        finally:
            seen.append(_heartbeat(sync_redis)["state"])
            await shutdown(ctx)
    finally:
        await ctx["redis"].aclose()


def test_heartbeat_goes_loading_then_ready(sync_redis: redis.Redis) -> None:
    seen: list[str] = []
    settings = make_worker_settings(fake_load_seconds=1)

    asyncio.run(_start_and_stop(settings, sync_redis, seen))

    assert seen == ["loading", "ready"]
    assert sync_redis.hlen(WORKERS_KEY) == 0


def test_ready_heartbeat_carries_model_info(sync_redis: redis.Redis) -> None:
    async def run() -> dict[str, Any]:
        settings = make_worker_settings()
        ctx: dict[str, Any] = {"redis": create_redis(settings.redis_url), "settings": settings}
        await startup(ctx)
        beat = _heartbeat(sync_redis)
        await shutdown(ctx)
        await ctx["redis"].aclose()
        return beat

    beat = asyncio.run(run())

    assert beat["state"] == "ready"
    assert beat["model"]["stage"] == "2b"
    assert beat["model"]["supported_languages"] == {
        "en": "English",
        "ig": "Igbo",
        "yo": "Yoruba",
        "ha": "Hausa",
    }


@pytest.mark.parametrize(
    ("error", "code"),
    [
        ("AuthenticationError", "model_auth_error"),
        ("GatedModelAccessError", "model_access_denied"),
        ("ModelLoadError", "model_load_error"),
    ],
)
def test_load_failure_publishes_failed_and_keeps_it(
    sync_redis: redis.Redis, error: str, code: str
) -> None:
    seen: list[str] = []
    settings = make_worker_settings(fake_load_seconds=0.5, fake_load_error=error)

    with pytest.raises(ModelStartupError) as excinfo:
        asyncio.run(_start_and_stop(settings, sync_redis, seen))

    assert excinfo.value.code == code
    assert seen == ["loading", "failed"]
    beat = _heartbeat(sync_redis)
    assert beat["state"] == "failed"
    assert beat["error_code"] == code


def test_only_auth_and_access_errors_are_permanent() -> None:
    assert {"model_auth_error", "model_access_denied"} == PERMANENT_LOAD_ERRORS


def test_park_keeps_a_fresh_failed_heartbeat_until_stopped(sync_redis: redis.Redis) -> None:
    async def run() -> dict[str, Any]:
        stop = asyncio.Event()
        task = asyncio.create_task(
            park(make_worker_settings(heartbeat_interval=1), "model_access_denied", stop)
        )
        await asyncio.sleep(1.5)
        beat = _heartbeat(sync_redis)
        stop.set()
        await task
        return beat

    beat = asyncio.run(run())

    assert beat["state"] == "failed"
    assert beat["error_code"] == "model_access_denied"
    assert time.time() - beat["updated_at"] < 1.5
    assert sync_redis.hlen(WORKERS_KEY) == 1


@pytest.mark.parametrize(
    ("code", "parks"),
    [("model_auth_error", True), ("model_access_denied", True), ("model_load_error", False)],
)
def test_main_parks_on_permanent_errors_and_exits_otherwise(
    monkeypatch: pytest.MonkeyPatch, code: str, parks: bool
) -> None:
    parked: list[str] = []

    class FailingWorker:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def run(self) -> None:
            raise ModelStartupError(code)

    async def fake_park(settings: Any, error_code: str) -> None:
        parked.append(error_code)

    monkeypatch.setattr(worker_entry, "Worker", FailingWorker)
    monkeypatch.setattr(worker_entry, "park", fake_park)
    monkeypatch.setattr(worker_entry, "get_worker_settings", make_worker_settings)

    if parks:
        worker_entry.main()
        assert parked == [code]
    else:
        with pytest.raises(SystemExit) as exit_info:
            worker_entry.main()
        assert exit_info.value.code == 1
        assert parked == []
