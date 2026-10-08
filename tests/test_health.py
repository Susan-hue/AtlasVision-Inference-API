from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.main import create_app
from tests.conftest import make_api_settings


def test_health_ok_without_api_key(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "redis": "ok"}


def test_health_reports_redis_down_without_internals() -> None:
    # Nothing listens on port 1, so the connection is refused immediately.
    app = create_app(make_api_settings(redis_url="redis://127.0.0.1:1/0"))

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 503
    # Exactly these keys: no versions, hostnames, or error text.
    assert response.json() == {"status": "degraded", "redis": "unavailable"}
