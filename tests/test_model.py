from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.schemas import WorkerState


def test_no_worker_is_unavailable(client: TestClient) -> None:
    assert client.get("/v1/model").json() == {
        "state": "unavailable",
        "model": None,
        "error_code": None,
    }


def test_ready_worker_reports_model_info(client: TestClient, publish_heartbeat) -> None:
    publish_heartbeat(WorkerState.READY, model={"stage": "2b", "device": "cuda"})

    assert client.get("/v1/model").json() == {
        "state": "ready",
        "model": {"stage": "2b", "device": "cuda"},
        "error_code": None,
    }


def test_loading_worker(client: TestClient, publish_heartbeat) -> None:
    publish_heartbeat(WorkerState.LOADING)

    assert client.get("/v1/model").json()["state"] == "loading"


def test_failed_worker_reports_error_code(client: TestClient, publish_heartbeat) -> None:
    publish_heartbeat(WorkerState.FAILED, error_code="model_access_denied")

    assert client.get("/v1/model").json() == {
        "state": "failed",
        "model": None,
        "error_code": "model_access_denied",
    }


def test_stale_heartbeat_is_unavailable(client: TestClient, publish_heartbeat) -> None:
    publish_heartbeat(WorkerState.READY, age=600)

    assert client.get("/v1/model").json()["state"] == "unavailable"
