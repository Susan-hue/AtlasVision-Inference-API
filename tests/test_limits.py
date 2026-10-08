from __future__ import annotations

import asyncio
from typing import Any

import pytest
import redis
from arq.constants import default_queue_name

from app.api.middleware import BodySizeLimit
from app.core.ratelimit import Limit, hit
from app.core.redis import create_redis
from app.core.schemas import WorkerState
from tests.conftest import TEST_REDIS_URL, api_client, image_bytes, make_api_settings, submit_chat

ALICE = {"X-Client-Id": "alice-00000001"}
BOB = {"X-Client-Id": "bob-000000001"}
CAROL = {"X-Client-Id": "carol-0000001"}


@pytest.fixture
def ready(publish_heartbeat) -> None:
    publish_heartbeat(WorkerState.READY)


def _assert_rate_limited(response: Any) -> None:
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limited"
    assert 1 <= int(response.headers["retry-after"]) <= 60


def test_submit_limit_is_per_client(sync_redis: redis.Redis, ready: None) -> None:
    with api_client(make_api_settings(rate_limit_submit_per_client=2)) as client:
        assert [submit_chat(client, headers=ALICE).status_code for _ in range(2)] == [202, 202]
        _assert_rate_limited(submit_chat(client, headers=ALICE))
        assert submit_chat(client, headers=BOB).status_code == 202

    assert sync_redis.zcard(default_queue_name) == 3


def test_submit_limit_per_key_covers_all_clients(sync_redis: redis.Redis, ready: None) -> None:
    settings = make_api_settings(rate_limit_submit_per_client=100, rate_limit_submit_per_key=3)
    with api_client(settings) as client:
        for headers in (ALICE, ALICE, BOB):
            assert submit_chat(client, headers=headers).status_code == 202
        _assert_rate_limited(submit_chat(client, headers=CAROL))


def test_polling_is_limited_separately_from_submitting(
    sync_redis: redis.Redis, ready: None
) -> None:
    settings = make_api_settings(rate_limit_submit_per_client=1, rate_limit_read_per_client=3)
    with api_client(settings) as client:
        job_id = submit_chat(client, headers=ALICE).json()["job_id"]
        _assert_rate_limited(submit_chat(client, headers=ALICE))

        polls = [client.get(f"/v1/jobs/{job_id}", headers=ALICE).status_code for _ in range(3)]
        assert polls == [200, 200, 200]
        _assert_rate_limited(client.get(f"/v1/jobs/{job_id}", headers=ALICE))
        _assert_rate_limited(client.get("/v1/model", headers=ALICE))


def test_limits_reset_in_the_next_window(sync_redis: redis.Redis) -> None:
    async def run() -> list[int | None]:
        client = create_redis(TEST_REDIS_URL)
        limits = [Limit("scope", 1)]
        try:
            return [
                await hit(client, "test", limits, now=120.0),
                await hit(client, "test", limits, now=150.5),
                await hit(client, "test", limits, now=180.0),
            ]
        finally:
            await client.aclose()

    assert asyncio.run(run()) == [None, 30, None]


def test_full_queue_returns_503(sync_redis: redis.Redis, ready: None) -> None:
    with api_client(make_api_settings(max_queue_depth=2)) as client:
        for _ in range(2):
            assert submit_chat(client).status_code == 202
        response = submit_chat(client)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "queue_full"
    assert response.headers["retry-after"] == "30"
    assert sync_redis.zcard(default_queue_name) == 2


def test_oversized_upload_is_rejected_with_413(sync_redis: redis.Redis, ready: None) -> None:
    settings = make_api_settings(max_image_mb=0.01)
    big = image_bytes() + b"\0" * (settings.max_request_bytes + 1)
    with api_client(settings) as client:
        response = submit_chat(client, image=big)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"
    assert sync_redis.zcard(default_queue_name) == 0


# --- BodySizeLimit at the ASGI level -----------------------------------------------------


def _call(
    middleware_limit: int, headers: list[tuple[bytes, bytes]], chunks: list[bytes]
) -> tuple[int, list[bytes], int]:
    """Returns (status, body chunks the app saw, number of receive() calls)."""
    received: list[bytes] = []
    calls = 0
    status = 0

    async def app(scope: Any, receive: Any, send: Any) -> None:
        message = await receive()
        received.append(message["body"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        index = calls - 1
        return {"type": "http.request", "body": chunks[index], "more_body": index < len(chunks) - 1}

    async def send(message: dict[str, Any]) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]

    scope = {"type": "http", "method": "POST", "path": "/", "headers": headers}
    asyncio.run(BodySizeLimit(app, max_bytes=middleware_limit)(scope, receive, send))
    return status, received, calls


def test_declared_oversize_is_rejected_before_reading() -> None:
    status, seen, calls = _call(10, [(b"content-length", b"11")], [b"x" * 11])

    assert (status, seen, calls) == (413, [], 0)


def test_streamed_oversize_stops_reading_at_the_limit() -> None:
    status, seen, calls = _call(10, [], [b"x" * 6, b"x" * 6, b"x" * 6, b"x" * 6])

    assert (status, seen, calls) == (413, [], 2)


def test_body_within_limit_is_replayed_whole() -> None:
    status, seen, _ = _call(10, [(b"content-length", b"10")], [b"12345", b"67890"])

    assert (status, seen) == (200, [b"1234567890"])
