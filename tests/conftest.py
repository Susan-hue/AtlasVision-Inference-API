from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.core.config import ApiSettings

# Compose points this at Redis DB 15; the fallback is for running against a local Redis.
TEST_REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/15")


def make_api_settings(**overrides: Any) -> ApiSettings:
    """Settings built from code only. `_env_file=None` ignores any local .env file."""
    return ApiSettings(_env_file=None, **{"redis_url": TEST_REDIS_URL, **overrides})


@pytest.fixture
def client() -> Iterator[TestClient]:
    # Using TestClient as a context manager runs the app's lifespan (Redis pool setup).
    with TestClient(create_app(make_api_settings())) as test_client:
        yield test_client
