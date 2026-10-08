from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
import redis
from arq.constants import default_queue_name
from fastapi.testclient import TestClient

from app.core.jobs import job_key, utcnow
from app.core.schemas import ChatParams, JobRecord, JobStatus, WorkerState
from app.core.sessions import Conversation, lock_key, session_key
from app.worker.fake_model import FakeAtlasVision
from tests.conftest import (
    make_api_settings,
    make_worker_settings,
    run_job,
    run_worker,
    submit_chat,
)

SETTINGS = make_api_settings()
QUEUED_LOCK_TTL = SETTINGS.image_ttl + SETTINGS.job_timeout + 60
RUNNING_LOCK_TTL = SETTINGS.job_timeout + 60
BUSY_MESSAGE = (
    "This conversation is still answering your previous question. "
    "Please wait for that answer before asking another."
)


@pytest.fixture
def ready(publish_heartbeat) -> None:
    publish_heartbeat(WorkerState.READY)


def new_session(client: TestClient) -> str:
    response = client.post("/v1/sessions")
    assert response.status_code == 201
    return response.json()["session_id"]


def history(sync_redis: redis.Redis, session_id: str) -> list[dict[str, str]]:
    return json.loads(sync_redis.get(session_key(session_id)))["history"]


def seed_history(sync_redis: redis.Redis, session_id: str, turns: int) -> None:
    numbers = range(1, turns + 1)
    data = {
        "session_id": session_id,
        "history": [{"question": f"q{i}", "answer": f"a{i}"} for i in numbers],
        "turns": [
            {"question": f"yo-q{i}", "question_lang": "yo", "lang": "yo", "answer": f"yo-a{i}"}
            for i in numbers
        ],
    }
    sync_redis.set(session_key(session_id), json.dumps(data))


def poll(client: TestClient, job_id: str) -> dict[str, Any]:
    return client.get(f"/v1/jobs/{job_id}").json()


# --- session endpoints -----------------------------------------------------------------


def test_create_session(client: TestClient, sync_redis: redis.Redis) -> None:
    response = client.post("/v1/sessions")

    session_id = response.json()["session_id"]
    assert len(session_id) == 32
    assert response.headers["location"] == f"/v1/sessions/{session_id}"
    assert history(sync_redis, session_id) == []
    assert 0 < sync_redis.ttl(session_key(session_id)) <= SETTINGS.session_ttl


def test_get_session(client: TestClient) -> None:
    session_id = new_session(client)

    response = client.get(f"/v1/sessions/{session_id}")

    assert response.status_code == 200
    assert response.json() == {"session_id": session_id, "history": [], "active_job_id": None}


def test_get_refreshes_ttl(client: TestClient, sync_redis: redis.Redis) -> None:
    session_id = new_session(client)
    sync_redis.expire(session_key(session_id), 10)

    client.get(f"/v1/sessions/{session_id}")

    assert sync_redis.ttl(session_key(session_id)) > 10


@pytest.mark.parametrize("method", ["get", "delete"])
@pytest.mark.parametrize("session_id", ["0" * 32, "not-a-session"])
def test_unknown_session_is_404(client: TestClient, method: str, session_id: str) -> None:
    response = client.request(method.upper(), f"/v1/sessions/{session_id}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "session_not_found"


def test_delete_session(client: TestClient, sync_redis: redis.Redis) -> None:
    session_id = new_session(client)

    assert client.delete(f"/v1/sessions/{session_id}").status_code == 204
    assert client.get(f"/v1/sessions/{session_id}").status_code == 404
    assert sync_redis.exists(session_key(session_id)) == 0


# --- chat jobs on a session ------------------------------------------------------------


def test_chat_with_unknown_session_is_404(
    client: TestClient, sync_redis: redis.Redis, ready: None
) -> None:
    response = submit_chat(client, session_id="0" * 32)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "session_not_found"
    assert sync_redis.zcard(default_queue_name) == 0


def test_blank_session_id_means_no_session(
    client: TestClient, sync_redis: redis.Redis, ready: None
) -> None:
    job_id = submit_chat(client, session_id="").json()["job_id"]

    record = JobRecord.model_validate_json(sync_redis.get(job_key(job_id)))
    assert record.session_id is None


def test_submit_locks_session_and_refreshes_ttl(
    client: TestClient, sync_redis: redis.Redis, ready: None
) -> None:
    session_id = new_session(client)
    sync_redis.expire(session_key(session_id), 10)

    job_id = submit_chat(client, session_id=session_id).json()["job_id"]

    assert sync_redis.get(lock_key(session_id)).decode() == job_id
    assert RUNNING_LOCK_TTL < sync_redis.ttl(lock_key(session_id)) <= QUEUED_LOCK_TTL
    assert sync_redis.ttl(session_key(session_id)) > 10
    assert poll(client, job_id)["session_id"] == session_id
    assert client.get(f"/v1/sessions/{session_id}").json()["active_job_id"] == job_id


def test_busy_session_returns_409_with_running_job(
    client: TestClient, sync_redis: redis.Redis, ready: None
) -> None:
    session_id = new_session(client)
    first = submit_chat(client, session_id=session_id).json()["job_id"]

    response = submit_chat(client, session_id=session_id)

    assert response.status_code == 409
    assert response.json() == {
        "error": {"code": "session_busy", "message": BUSY_MESSAGE, "job_id": first}
    }
    assert sync_redis.zcard(default_queue_name) == 1


def _record(job_id: str, **updates: Any) -> JobRecord:
    params = ChatParams(question="q", lang="en", question_lang="en", max_new_tokens=10)
    base = JobRecord(job_id=job_id, status=JobStatus.QUEUED, created_at=utcnow(), params=params)
    return base.model_copy(update=updates)


@pytest.mark.parametrize(
    "holder",
    [
        None,
        _record("b" * 32, created_at=utcnow() - timedelta(seconds=SETTINGS.image_ttl + 60)),
        _record("b" * 32, status=JobStatus.FAILED),
    ],
    ids=["record-gone", "expired-in-queue", "already-failed"],
)
def test_lock_held_by_dead_job_is_taken_over(
    client: TestClient, sync_redis: redis.Redis, ready: None, holder: JobRecord | None
) -> None:
    session_id = new_session(client)
    sync_redis.set(lock_key(session_id), "b" * 32)
    if holder is not None:
        sync_redis.set(job_key(holder.job_id), holder.model_dump_json())

    response = submit_chat(client, session_id=session_id)

    assert response.status_code == 202
    assert sync_redis.get(lock_key(session_id)).decode() == response.json()["job_id"]


# --- worker side -----------------------------------------------------------------------


def test_conversation_continues_across_jobs(
    client: TestClient, sync_redis: redis.Redis, ready: None
) -> None:
    session_id = new_session(client)

    first = submit_chat(client, session_id=session_id, question="What is it?").json()["job_id"]
    run_worker(make_worker_settings())
    assert sync_redis.exists(lock_key(session_id)) == 0

    second = submit_chat(client, session_id=session_id, question="What colour?").json()["job_id"]
    run_worker(make_worker_settings())

    assert poll(client, first)["result"]["english_answer"].startswith("Fake answer 1 ")
    assert poll(client, second)["result"]["english_answer"].startswith("Fake answer 2 ")
    body = client.get(f"/v1/sessions/{session_id}").json()
    assert [t["question"] for t in body["history"]] == ["What is it?", "What colour?"]
    assert body["active_job_id"] is None


def test_history_is_trimmed_before_each_call(
    client: TestClient, sync_redis: redis.Redis, ready: None
) -> None:
    session_id = new_session(client)
    seed_history(sync_redis, session_id, turns=10)

    job_id = submit_chat(client, session_id=session_id, question="Next?").json()["job_id"]
    run_worker(make_worker_settings())

    # The model saw 8 earlier turns, so this is its 9th answer.
    assert poll(client, job_id)["result"]["english_answer"].startswith("Fake answer 9 ")
    stored = history(sync_redis, session_id)
    assert len(stored) == 8
    assert stored[0]["question"] == "q4"
    assert stored[-1]["question"] == "Next?"
    turns = client.get(f"/v1/sessions/{session_id}").json()["history"]
    assert [t["question"] for t in turns] == [f"yo-q{i}" for i in range(4, 11)] + ["Next?"]
    assert turns[0]["english_question"] == "q4"


def test_failed_job_keeps_history_and_releases_lock(
    client: TestClient, sync_redis: redis.Redis, ready: None
) -> None:
    session_id = new_session(client)
    seed_history(sync_redis, session_id, turns=2)

    job_id = submit_chat(client, session_id=session_id, question="__raise_ValueError__").json()[
        "job_id"
    ]
    run_worker(make_worker_settings())

    assert poll(client, job_id)["error"]["code"] == "invalid_input"
    assert len(history(sync_redis, session_id)) == 2
    assert len(client.get(f"/v1/sessions/{session_id}").json()["history"]) == 2
    assert sync_redis.exists(lock_key(session_id)) == 0


def test_session_deleted_before_job_runs(
    client: TestClient, sync_redis: redis.Redis, ready: None
) -> None:
    session_id = new_session(client)
    job_id = submit_chat(client, session_id=session_id).json()["job_id"]
    client.delete(f"/v1/sessions/{session_id}")

    run_worker(make_worker_settings())

    assert poll(client, job_id)["error"]["code"] == "session_not_found"
    assert sync_redis.exists(session_key(session_id), lock_key(session_id)) == 0


class HookedModel(FakeAtlasVision):
    """Runs `hook` inside chat(), i.e. while the job is in progress on the worker thread."""

    def __init__(self, hook: Any) -> None:
        super().__init__()
        self.hook = hook

    def chat(self, *args: Any, **kwargs: Any) -> Any:
        self.hook()
        return super().chat(*args, **kwargs)


def test_worker_shortens_lock_ttl_when_job_starts(
    client: TestClient, sync_redis: redis.Redis, ready: None
) -> None:
    session_id = new_session(client)
    job_id = submit_chat(client, session_id=session_id).json()["job_id"]
    seen: list[int] = []

    run_job(job_id, HookedModel(lambda: seen.append(sync_redis.ttl(lock_key(session_id)))))

    assert 0 < seen[0] <= RUNNING_LOCK_TTL
    assert sync_redis.exists(lock_key(session_id)) == 0


def test_session_deleted_while_job_runs(
    client: TestClient, sync_redis: redis.Redis, ready: None
) -> None:
    session_id = new_session(client)
    job_id = submit_chat(client, session_id=session_id).json()["job_id"]

    run_job(job_id, HookedModel(lambda: client.delete(f"/v1/sessions/{session_id}")))

    body = poll(client, job_id)
    assert body["status"] == "done"
    assert body["result"]["answer"]
    assert sync_redis.exists(session_key(session_id), lock_key(session_id)) == 0


def test_history_keeps_original_question_and_translated_answer(
    client: TestClient, ready: None
) -> None:
    session_id = new_session(client)
    submit_chat(client, session_id=session_id, question="Kini eyi?", lang="yo", question_lang="yo")
    run_worker(make_worker_settings())
    submit_chat(
        client, session_id=session_id, question="What colour?", lang="ha", question_lang="en"
    )
    run_worker(make_worker_settings())

    assert client.get(f"/v1/sessions/{session_id}").json()["history"] == [
        {
            "question": "Kini eyi?",
            "question_lang": "yo",
            "lang": "yo",
            "answer": "[en->yo] Fake answer 1 about a 32x24 image: [yo->en] Kini eyi?",
            "english_question": "[yo->en] Kini eyi?",
            "english_answer": "Fake answer 1 about a 32x24 image: [yo->en] Kini eyi?",
        },
        {
            "question": "What colour?",
            "question_lang": "en",
            "lang": "ha",
            "answer": "[en->ha] Fake answer 2 about a 32x24 image: What colour?",
            "english_question": "What colour?",
            "english_answer": "Fake answer 2 about a 32x24 image: What colour?",
        },
    ]


def _turn(i: int) -> dict[str, str]:
    return {"question": f"t{i}", "question_lang": "ig", "lang": "ig", "answer": f"t{i}"}


@pytest.mark.parametrize(
    ("turns", "expected"),
    [
        ([_turn(3)], ["q1", "q2", "t3"]),
        ([_turn(i) for i in range(5)], ["t2", "t3", "t4"]),
    ],
    ids=["fewer-turns-than-history", "more-turns-than-history"],
)
def test_conversation_realigns_turns_with_history(
    turns: list[dict[str, str]], expected: list[str]
) -> None:
    data = {
        "session_id": "s",
        "history": [{"question": f"q{i}", "answer": f"a{i}"} for i in (1, 2, 3)],
        "turns": turns,
    }

    conversation = Conversation.from_dict(data)

    assert [t.question for t in conversation.turns] == expected
    assert conversation.turns[0].lang == ("en" if expected[0] == "q1" else "ig")
