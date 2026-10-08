from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
import redis
from arq.constants import default_queue_name
from pydantic import SecretStr

from app.api.auth import EnvKeyStore, key_id
from app.api.middleware import BodySizeLimit
from app.core.ratelimit import Limit, hit
from app.core.redis import create_redis
from app.core.schemas import WorkerState
from tests.conftest import (
    TEST_KEY,
    TEST_REDIS_URL,
    api_client,
    image_bytes,
    make_api_settings,
    submit_chat,
)

ALICE = {"X-Client-Id": "alice-00000001"}
BOB = {"X-Client-Id": "bob-000000001"}
CAROL = {"X-Client-Id": "carol-0000001"}


@pytest.fixture
def ready(publish_heartbeat) -> None:
    publish_heartbeat(WorkerState.READY)


def _assert_rate_limited(response: Any) -> None:
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limited"
    # Up to two windows: once a minute's count is over the limit, it frees up next minute.
    assert 1 <= int(response.headers["retry-after"]) <= 120


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


def _hits(limit: int, times: list[float]) -> list[int | None]:
    """Send one request at each time against a fresh limit; return each Retry-After."""
    bucket = f"test-{uuid.uuid4().hex}"

    async def run() -> list[int | None]:
        client = create_redis(TEST_REDIS_URL)
        try:
            return [await hit(client, bucket, [Limit("scope", limit)], now=t) for t in times]
        finally:
            await client.aclose()

    return asyncio.run(run())


def test_burst_across_a_window_boundary_is_blocked(sync_redis: redis.Redis) -> None:
    # 10 requests at the end of one minute, then more right after it starts. A fixed
    # window would allow 10 more; the sliding window still counts the first 10.
    results = _hits(10, [59.0] * 10 + [61.0])

    assert results[:10] == [None] * 10
    assert results[10] is not None


def test_previous_window_weighs_less_as_the_window_moves(sync_redis: redis.Redis) -> None:
    # Halfway through the next minute the previous 10 count as 5, leaving room for 5 more.
    results = _hits(10, [30.0] * 10 + [90.0] * 6)

    assert results[10:15] == [None] * 5
    assert results[15] is not None


@pytest.mark.parametrize(
    ("limit", "burst", "rejected_at"),
    [(10, [59.0] * 10, 61.0), (2, [10.0] * 2, 10.0)],
    ids=["frees-up-in-this-window", "frees-up-in-the-next-window"],
)
def test_retry_after_is_accurate(
    sync_redis: redis.Redis, limit: int, burst: list[float], rejected_at: float
) -> None:
    retry_after = _hits(limit, [*burst, rejected_at])[-1]
    assert retry_after is not None

    # Replays of the same history: one second early is still too soon; on time succeeds.
    early = _hits(limit, [*burst, rejected_at, rejected_at + retry_after - 1])[-1]
    on_time = _hits(limit, [*burst, rejected_at, rejected_at + retry_after])[-1]

    assert early is not None
    assert on_time is None


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


KEY_HEADER = (b"x-api-key", TEST_KEY.encode())


def _call(
    middleware_limit: int,
    headers: list[tuple[bytes, bytes]],
    chunks: list[bytes],
    path: str = "/v1/jobs/chat",
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

    scope = {"type": "http", "method": "POST", "path": path, "headers": headers}
    store = EnvKeyStore([SecretStr(TEST_KEY)])
    middleware = BodySizeLimit(app, max_bytes=middleware_limit, key_store=store)
    asyncio.run(middleware(scope, receive, send))
    assert scope.get("state", {}).get("key_id") in (None, key_id(TEST_KEY))
    return status, received, calls


def test_declared_oversize_is_rejected_before_reading() -> None:
    status, seen, calls = _call(10, [KEY_HEADER, (b"content-length", b"11")], [b"x" * 11])

    assert (status, seen, calls) == (413, [], 0)


def test_streamed_oversize_stops_reading_at_the_limit() -> None:
    status, seen, calls = _call(10, [KEY_HEADER], [b"x" * 6, b"x" * 6, b"x" * 6, b"x" * 6])

    assert (status, seen, calls) == (413, [], 2)


def test_body_within_limit_is_replayed_whole() -> None:
    status, seen, _ = _call(10, [KEY_HEADER, (b"content-length", b"10")], [b"12345", b"67890"])

    assert (status, seen) == (200, [b"1234567890"])


@pytest.mark.parametrize(
    "headers",
    [[], [(b"x-api-key", b"wrong-key")], [(b"x-api-key", TEST_KEY.encode() + b"x")]],
    ids=["no-key", "wrong-key", "almost-right-key"],
)
def test_body_is_never_read_without_a_valid_key(headers: list[tuple[bytes, bytes]]) -> None:
    status, seen, calls = _call(10, [*headers, (b"content-length", b"5")], [b"small"])

    assert (status, seen, calls) == (401, [], 0)


def test_health_needs_no_key() -> None:
    status, _, _ = _call(10, [], [b""], path="/health")

    assert status == 200


def test_unauthenticated_upload_gets_401_not_413(sync_redis: redis.Redis) -> None:
    big = image_bytes() + b"\0" * (make_api_settings().max_request_bytes + 1)
    with api_client(key=None) as client:
        response = submit_chat(client, image=big)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize("path", ["/docs", "/openapi.json"])
def test_docs_need_a_key(sync_redis: redis.Redis, path: str) -> None:
    with api_client(key=None) as anonymous:
        assert anonymous.get(path).status_code == 401
    with api_client() as client:
        assert client.get(path).status_code == 200
