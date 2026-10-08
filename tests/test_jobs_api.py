from __future__ import annotations

import os
from datetime import timedelta

import pytest
import redis
from arq.constants import default_queue_name
from fastapi.testclient import TestClient

from app.core.jobs import image_key, job_key, utcnow
from app.core.schemas import ChatParams, JobRecord, JobStatus, WorkerState
from tests.conftest import TEST_OWNER, api_client, image_bytes, make_api_settings, submit_chat


@pytest.fixture
def ready(publish_heartbeat) -> None:
    publish_heartbeat(WorkerState.READY)


def test_submit_stores_image_and_queues_job(
    client: TestClient, sync_redis: redis.Redis, ready: None
) -> None:
    response = submit_chat(client, lang="Yoruba")

    assert response.status_code == 202
    body = response.json()
    job_id = body["job_id"]
    assert body == {"job_id": job_id, "status": "queued"}
    assert response.headers["location"] == f"/v1/jobs/{job_id}"

    assert sync_redis.get(image_key(job_id)) == image_bytes()
    assert 0 < sync_redis.ttl(image_key(job_id)) <= make_api_settings().image_ttl
    record = JobRecord.model_validate_json(sync_redis.get(job_key(job_id)))
    assert record.status is JobStatus.QUEUED
    assert record.params == ChatParams(
        question="What is this?", lang="yo", question_lang="en", max_new_tokens=256
    )
    assert sync_redis.zcard(default_queue_name) == 1


def test_poll_queued_job(client: TestClient, ready: None) -> None:
    job_id = submit_chat(client).json()["job_id"]

    response = client.get(f"/v1/jobs/{job_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued"
    assert body["result"] is None
    assert body["error"] is None
    assert "params" not in body


@pytest.mark.parametrize("job_id", ["0" * 32, "not-a-job-id", "../../etc"])
def test_unknown_job_is_404(client: TestClient, job_id: str) -> None:
    response = client.get(f"/v1/jobs/{job_id}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] in {"job_not_found", "not_found"}


@pytest.mark.parametrize(
    ("state", "age"),
    [(None, 0), (WorkerState.FAILED, 0), (WorkerState.READY, 60)],
    ids=["no-worker", "failed", "stale"],
)
def test_rejects_jobs_when_worker_unavailable(
    client: TestClient, sync_redis: redis.Redis, publish_heartbeat, state, age
) -> None:
    if state is not None:
        publish_heartbeat(state, age=age)

    response = submit_chat(client)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "model_unavailable"
    assert "retry-after" in response.headers
    assert sync_redis.zcard(default_queue_name) == 0


def test_accepts_jobs_while_worker_loading(client: TestClient, publish_heartbeat) -> None:
    publish_heartbeat(WorkerState.LOADING)

    assert submit_chat(client).status_code == 202


@pytest.mark.parametrize(
    ("fields", "status", "code"),
    [
        ({"lang": "fr"}, 422, "unsupported_language"),
        ({"question_lang": "xx"}, 422, "unsupported_language"),
        ({"lang": "yo", "question_lang": "ha"}, 422, "invalid_question_lang"),
        ({"question": "   "}, 422, "invalid_question"),
        ({"question": "x" * 1001}, 422, "question_too_long"),
    ],
)
def test_field_validation(
    client: TestClient, sync_redis: redis.Redis, ready: None, fields, status, code
) -> None:
    response = submit_chat(client, **fields)

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert sync_redis.zcard(default_queue_name) == 0


@pytest.mark.parametrize(
    ("image", "content_type", "status", "code"),
    [
        (b"hello", "text/plain", 415, "unsupported_media_type"),
        (b"not really a png", "image/png", 422, "invalid_image"),
        (image_bytes(fmt="JPEG"), "image/png", 422, "invalid_image"),
        (image_bytes(fmt="PNG")[:40], "image/png", 422, "invalid_image"),
    ],
    ids=["text", "garbage", "mismatched-type", "truncated"],
)
def test_image_validation(
    client: TestClient, ready: None, image: bytes, content_type: str, status: int, code: str
) -> None:
    response = submit_chat(client, image=image, content_type=content_type)

    assert response.status_code == status
    assert response.json()["error"]["code"] == code


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP"])
def test_accepts_supported_formats(client: TestClient, ready: None, fmt: str) -> None:
    content_type = f"image/{fmt.lower()}"

    assert (
        submit_chat(client, image=image_bytes(fmt=fmt), content_type=content_type).status_code
        == 202
    )


def test_rejects_oversized_image(sync_redis: redis.Redis, publish_heartbeat) -> None:
    publish_heartbeat(WorkerState.READY)
    noisy = image_bytes(300, 300, "PNG") + os.urandom(2048)
    with api_client(make_api_settings(max_image_mb=0.001)) as client:
        response = submit_chat(client, image=noisy)

    assert response.status_code == 413
    # Image over MAX_IMAGE_MB but whole body within the request cap: same code as the cap.
    assert response.json()["error"]["code"] == "request_too_large"


def test_missing_image_uses_error_shape(client: TestClient, ready: None) -> None:
    response = client.post(
        "/v1/jobs/chat", data={"question": "Hi", "lang": "en", "question_lang": "en"}
    )

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "invalid_request",
            "message": "A field is missing or invalid.",
            "field": "image",
            "problem": "Field required",
        }
    }


@pytest.mark.parametrize("fields", [{"question": ""}, {"question": "   "}, {}])
def test_empty_or_missing_question_is_invalid_question(
    client: TestClient, ready: None, fields: dict[str, str]
) -> None:
    data = {"lang": "en", "question_lang": "en", **fields}
    files = {"image": ("photo", image_bytes(), "image/png")}

    response = client.post("/v1/jobs/chat", data=data, files=files)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_question"


def test_error_details_go_in_extra_fields(client: TestClient, ready: None) -> None:
    too_long = submit_chat(client, question="x" * 1001).json()["error"]
    bad_lang = submit_chat(client, question_lang="xx").json()["error"]

    assert too_long == {
        "code": "question_too_long",
        "message": "question is too long.",
        "max_length": 1000,
    }
    assert bad_lang == {
        "code": "unsupported_language",
        "message": "Language must be one of en, ig, yo, ha.",
        "field": "question_lang",
    }


def test_unparseable_body_is_bad_request(client: TestClient) -> None:
    response = client.post(
        "/v1/jobs/chat",
        content=b"--x\r\nnot multipart at all",
        headers={"Content-Type": "multipart/form-data; boundary=x"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


@pytest.mark.parametrize(
    ("setting", "value", "prepare"),
    [
        ("retry_after_model_unavailable", 7, "no-worker"),
        ("retry_after_queue_full", 11, "full-queue"),
    ],
)
def test_retry_after_comes_from_settings(
    sync_redis: redis.Redis, publish_heartbeat, setting: str, value: int, prepare: str
) -> None:
    if prepare == "full-queue":
        publish_heartbeat(WorkerState.READY)
    settings = make_api_settings(max_queue_depth=1, **{setting: value})
    with api_client(settings) as client:
        if prepare == "full-queue":
            submit_chat(client)
        response = submit_chat(client)

    assert response.status_code == 503
    assert response.headers["retry-after"] == str(value)


def _store(sync_redis: redis.Redis, record: JobRecord) -> None:
    sync_redis.set(job_key(record.job_id), record.model_dump_json())


def _record(**updates: object) -> JobRecord:
    params = ChatParams(question="q", lang="en", question_lang="en", max_new_tokens=10)
    base = JobRecord(
        job_id="a" * 32,
        status=JobStatus.QUEUED,
        owner=TEST_OWNER,
        created_at=utcnow(),
        params=params,
    )
    return base.model_copy(update=updates)


def test_job_queued_past_image_ttl_reports_expired(
    client: TestClient, sync_redis: redis.Redis
) -> None:
    old = utcnow() - timedelta(seconds=make_api_settings().image_ttl + 60)
    _store(sync_redis, _record(created_at=old))

    body = client.get(f"/v1/jobs/{'a' * 32}").json()

    assert body["status"] == "failed"
    assert body["error"]["code"] == "job_expired"


def test_job_running_past_timeout_reports_worker_lost(
    client: TestClient, sync_redis: redis.Redis
) -> None:
    started = utcnow() - timedelta(seconds=make_api_settings().job_timeout + 60)
    _store(sync_redis, _record(status=JobStatus.RUNNING, started_at=started))

    body = client.get(f"/v1/jobs/{'a' * 32}").json()

    assert body["status"] == "failed"
    assert body["error"]["code"] == "worker_lost"
