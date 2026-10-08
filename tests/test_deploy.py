from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.core.config import SharedSettings

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def prod() -> dict[str, Any]:
    return yaml.safe_load((ROOT / "docker-compose.prod.yml").read_text())


def _default(value: str) -> str:
    match = re.fullmatch(r"\$\{\w+:-([^}]*)\}", value)
    assert match, f"{value!r} has no default"
    return match.group(1)


def test_shared_settings_reach_both_services_with_the_code_defaults(prod: dict[str, Any]) -> None:
    shared = prod["x-shared-env"]
    fields = {name.upper(): field for name, field in SharedSettings.model_fields.items()}

    assert set(shared) == set(fields)
    for name, value in shared.items():
        if name == "REDIS_URL":
            continue
        expected = fields[name].default
        expected = str(expected).lower() if isinstance(expected, bool) else str(expected)
        assert _default(value) == expected, name
    assert prod["services"]["api"]["environment"] == shared
    assert prod["services"]["worker"]["environment"] == shared


def test_only_the_api_publishes_a_port(prod: dict[str, Any]) -> None:
    published = {name for name, service in prod["services"].items() if service.get("ports")}

    assert published == {"api"}


def test_worker_reads_only_its_own_env_file(prod: dict[str, Any]) -> None:
    worker = prod["services"]["worker"]

    assert worker["env_file"] == ".env.worker"
    assert worker["build"]["target"] == "gpu"
    assert worker["volumes"] == ["hf-cache:/models/hf"]
    device = worker["deploy"]["resources"]["reservations"]["devices"][0]
    assert device["driver"] == "nvidia"
    assert device["capabilities"] == ["gpu"]


def test_redis_requires_a_password_and_persists(prod: dict[str, Any]) -> None:
    redis = prod["services"]["redis"]
    script = redis["command"][-1]

    assert "${REDIS_PASSWORD:?" in redis["environment"]["REDIS_PASSWORD"]
    assert "requirepass $$REDIS_PASSWORD" in script
    assert "maxmemory-policy volatile-lru" in script
    assert "appendonly yes" in script
    assert redis["volumes"] == ["redis-data:/data"]


def test_gpu_lock_is_hashed_on_transformers_4_and_free_of_torchao() -> None:
    lock = (ROOT / "requirements" / "worker-gpu.txt").read_text()
    pins = dict(re.findall(r"^([a-z0-9][a-z0-9._-]*)==(\S+)", lock, re.M))

    assert pins["testatlasvision"] == "0.1.0"
    assert pins["transformers"].startswith("4.")
    assert "torch" in pins
    assert "bitsandbytes" in pins
    assert "torchao" not in pins
    assert lock.count("--hash=sha256:") >= len(pins)
