from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from testatlasvision import (
    AuthenticationError,
    GatedModelAccessError,
    ImageLoadError,
    ModelLoadError,
    UnsupportedLanguageError,
)

from app.api.main import create_app
from app.core.errors import ERRORS, code_for
from app.core.schemas import WorkerState
from tests.conftest import make_api_settings, make_worker_settings, run_worker, submit_chat

EXPECTED_CODES = {
    "ImageLoadError": "invalid_image",
    "UnsupportedLanguageError": "unsupported_language",
    "ValueError": "invalid_input",
    "AuthenticationError": "model_unavailable",
    "GatedModelAccessError": "model_unavailable",
    "ModelLoadError": "model_unavailable",
    "RuntimeError": "internal_error",
}


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (ImageLoadError("x"), "invalid_image"),
        (UnsupportedLanguageError("x"), "unsupported_language"),
        (ValueError("x"), "invalid_input"),
        (AuthenticationError("x"), "model_unavailable"),
        (GatedModelAccessError("x"), "model_unavailable"),
        (ModelLoadError("x"), "model_unavailable"),
        (RuntimeError("x"), "internal_error"),
        (KeyError("x"), "internal_error"),
    ],
)
def test_code_for(exc: Exception, code: str) -> None:
    assert code_for(exc) == code


@pytest.mark.parametrize(("error", "code"), EXPECTED_CODES.items())
def test_job_failures_are_mapped_in_poll_response(
    client: TestClient, publish_heartbeat, error: str, code: str
) -> None:
    publish_heartbeat(WorkerState.READY)
    job_id = submit_chat(client, question=f"__raise_{error}__").json()["job_id"]

    run_worker(make_worker_settings())

    response = client.get(f"/v1/jobs/{job_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"] == {"code": code, "message": ERRORS[code].message}
    assert "Fake" not in response.text


def _broken_app(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> TestClient:
    async def boom(*args: object, **kwargs: object) -> None:
        raise exc

    monkeypatch.setattr("app.api.routes.sessions.create_session", boom)
    return TestClient(create_app(make_api_settings()), raise_server_exceptions=False)


def test_unexpected_api_error_is_generic_500(monkeypatch: pytest.MonkeyPatch) -> None:
    with _broken_app(monkeypatch, RuntimeError("secret detail")) as client:
        response = client.post("/v1/sessions")

    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "internal_error", "message": ERRORS["internal_error"].message}
    }


def test_sdk_error_in_api_uses_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    with _broken_app(monkeypatch, GatedModelAccessError("secret detail")) as client:
        response = client.post("/v1/sessions")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "model_unavailable"
    assert "secret" not in response.text
