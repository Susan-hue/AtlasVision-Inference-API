from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.auth import EnvKeyStore, key_id
from app.api.main import create_app
from app.core.schemas import WorkerState
from tests.conftest import OTHER_KEY, TEST_KEY, api_client, make_api_settings, submit_chat

PROTECTED = [
    ("POST", "/v1/jobs/chat"),
    ("GET", f"/v1/jobs/{'a' * 32}"),
    ("POST", "/v1/sessions"),
    ("GET", f"/v1/sessions/{'a' * 32}"),
    ("DELETE", f"/v1/sessions/{'a' * 32}"),
    ("GET", "/v1/model"),
]


@pytest.mark.parametrize(("method", "path"), PROTECTED)
@pytest.mark.parametrize("key", [None, "", "wrong-key", TEST_KEY + "x", TEST_KEY[:-1]])
def test_protected_endpoints_need_a_valid_key(
    sync_redis, method: str, path: str, key: str | None
) -> None:
    with api_client(key=key) as client:
        response = client.request(method, path)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert response.headers["www-authenticate"] == "ApiKey"


def test_health_is_open(sync_redis) -> None:
    with api_client(key=None) as client:
        assert client.get("/health").json() == {"status": "ok", "redis": "ok"}


@pytest.mark.parametrize("key", [TEST_KEY, OTHER_KEY])
def test_every_configured_key_works(sync_redis, publish_heartbeat, key: str) -> None:
    publish_heartbeat(WorkerState.READY)
    with api_client(key=key) as client:
        assert submit_chat(client).status_code == 202


@pytest.mark.parametrize(
    "client_id", ["short", "has space here", "x" * 129, "slash/not-ok", "émoji-🙂-id".encode()]
)
def test_malformed_client_id_is_rejected(client: TestClient, client_id: str | bytes) -> None:
    response = client.get("/v1/model", headers={"X-Client-Id": client_id})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_client_id"


@pytest.mark.parametrize(
    "client_id", ["3f2b8c1e-9d4a-4e6b-a1c2-7f0e9d8b6a5c", "abc_DEF-123", "x" * 128]
)
def test_well_formed_client_id_is_accepted(client: TestClient, client_id: str) -> None:
    assert client.get("/v1/model", headers={"X-Client-Id": client_id}).status_code == 200


def test_client_id_is_not_checked_without_a_valid_key(sync_redis) -> None:
    with api_client(key="wrong-key") as client:
        response = client.get("/v1/model", headers={"X-Client-Id": "bad"})

    assert response.status_code == 401


def test_env_key_store_returns_key_ids() -> None:
    store = EnvKeyStore([SecretStr("alpha-key"), SecretStr("beta-key")])

    async def lookups() -> list[str | None]:
        return [await store.lookup(k) for k in ("alpha-key", "beta-key", "gamma-key", "")]

    assert asyncio.run(lookups()) == [key_id("alpha-key"), key_id("beta-key"), None, None]


def test_key_id_does_not_contain_the_key() -> None:
    assert TEST_KEY not in key_id(TEST_KEY)
    assert len(key_id(TEST_KEY)) == 16


def test_api_refuses_to_start_without_keys() -> None:
    with pytest.raises(RuntimeError, match="API_KEYS"):
        create_app(make_api_settings(api_keys=[]))
