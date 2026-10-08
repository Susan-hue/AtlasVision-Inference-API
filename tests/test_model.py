from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.schemas import WorkerState

LANGS = {"en": "English", "ig": "Igbo", "yo": "Yoruba", "ha": "Hausa"}


def test_no_worker_is_unavailable(client: TestClient) -> None:
    assert client.get("/v1/model").json() == {
        "state": "unavailable",
        "stage": None,
        "supported_languages": None,
    }


def test_ready_worker_reports_only_public_details(client: TestClient, publish_heartbeat) -> None:
    info = {
        "stage": "2b",
        "llm": "NCAIR1/N-ATLaS",
        "device": "cuda:0",
        "supported_languages": LANGS,
    }
    publish_heartbeat(WorkerState.READY, model=info)

    assert client.get("/v1/model").json() == {
        "state": "ready",
        "stage": "2b",
        "supported_languages": LANGS,
    }


def test_loading_worker(client: TestClient, publish_heartbeat) -> None:
    publish_heartbeat(WorkerState.LOADING)

    assert client.get("/v1/model").json()["state"] == "loading"


def test_failed_worker_hides_the_reason(client: TestClient, publish_heartbeat) -> None:
    publish_heartbeat(WorkerState.FAILED, error_code="model_access_denied")

    assert client.get("/v1/model").json() == {
        "state": "failed",
        "stage": None,
        "supported_languages": None,
    }


def test_stale_heartbeat_is_unavailable(client: TestClient, publish_heartbeat) -> None:
    publish_heartbeat(WorkerState.READY, age=600)

    assert client.get("/v1/model").json()["state"] == "unavailable"
