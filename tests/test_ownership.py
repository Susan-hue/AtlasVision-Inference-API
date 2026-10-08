from __future__ import annotations

import pytest
import redis
from fastapi.testclient import TestClient

from app.api.auth import key_id
from app.core.jobs import job_key
from app.core.schemas import JobRecord, Owner, WorkerState
from app.core.sessions import session_key
from tests.conftest import OTHER_KEY, TEST_KEY, api_client, submit_chat

ALICE = {"X-Client-Id": "alice-00000001"}
BOB = {"X-Client-Id": "bob-000000001"}

STRANGERS = [
    (TEST_KEY, BOB),
    (TEST_KEY, {}),
    (OTHER_KEY, ALICE),
]
STRANGER_IDS = ["other-client", "key-without-client", "other-key-same-client"]


@pytest.fixture
def ready(publish_heartbeat) -> None:
    publish_heartbeat(WorkerState.READY)


def test_job_records_owner(client: TestClient, sync_redis: redis.Redis, ready: None) -> None:
    job_id = submit_chat(client, headers=ALICE).json()["job_id"]

    record = JobRecord.model_validate_json(sync_redis.get(job_key(job_id)))
    assert record.owner == Owner(key_id=key_id(TEST_KEY), client_id="alice-00000001")


def test_missing_client_id_means_the_key_is_the_client(
    client: TestClient, sync_redis: redis.Redis, ready: None
) -> None:
    job_id = submit_chat(client).json()["job_id"]

    record = JobRecord.model_validate_json(sync_redis.get(job_key(job_id)))
    assert record.owner == Owner(key_id=key_id(TEST_KEY), client_id=None)
    assert client.get(f"/v1/jobs/{job_id}").status_code == 200


@pytest.mark.parametrize(("key", "headers"), STRANGERS, ids=STRANGER_IDS)
def test_other_callers_cannot_see_a_job(
    client: TestClient, sync_redis, ready: None, key: str, headers: dict[str, str]
) -> None:
    job_id = submit_chat(client, headers=ALICE).json()["job_id"]

    with api_client(key=key) as stranger:
        response = stranger.get(f"/v1/jobs/{job_id}", headers=headers)
        missing = stranger.get(f"/v1/jobs/{'0' * 32}", headers=headers)

    assert response.status_code == 404
    assert response.json() == missing.json()
    assert client.get(f"/v1/jobs/{job_id}", headers=ALICE).status_code == 200


@pytest.mark.parametrize(("key", "headers"), STRANGERS, ids=STRANGER_IDS)
def test_other_callers_cannot_use_a_session(
    client: TestClient,
    sync_redis: redis.Redis,
    ready: None,
    key: str,
    headers: dict[str, str],
) -> None:
    session_id = client.post("/v1/sessions", headers=ALICE).json()["session_id"]

    with api_client(key=key) as stranger:
        get = stranger.get(f"/v1/sessions/{session_id}", headers=headers)
        delete = stranger.delete(f"/v1/sessions/{session_id}", headers=headers)
        chat = submit_chat(stranger, headers=headers, session_id=session_id)

    for response in (get, delete, chat):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "session_not_found"
    assert sync_redis.exists(session_key(session_id)) == 1
    assert client.get(f"/v1/sessions/{session_id}", headers=ALICE).status_code == 200


def test_owner_can_use_own_session(client: TestClient, ready: None) -> None:
    session_id = client.post("/v1/sessions", headers=ALICE).json()["session_id"]

    assert submit_chat(client, headers=ALICE, session_id=session_id).status_code == 202
    assert client.delete(f"/v1/sessions/{session_id}", headers=ALICE).status_code == 204
