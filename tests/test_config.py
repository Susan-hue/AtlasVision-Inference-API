from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import ApiSettings, WorkerSettings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test with none of our settings in the environment."""
    names = set(ApiSettings.model_fields) | set(WorkerSettings.model_fields)
    for name in names:
        monkeypatch.delenv(name.upper(), raising=False)


def test_api_defaults() -> None:
    settings = ApiSettings(_env_file=None)

    assert settings.image_ttl == 15 * 60
    assert settings.result_ttl == 60 * 60
    assert settings.session_ttl == 24 * 60 * 60
    assert settings.max_session_turns == 8
    assert settings.max_image_bytes == 4 * 1024 * 1024
    assert settings.rate_limit == 60
    assert settings.rate_limit_poll == 1200
    assert settings.api_keys == []
    assert settings.cors_origins == []
    assert settings.log_content is False


def test_comma_separated_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_KEYS", " key-one, key-two ,,")
    monkeypatch.setenv("CORS_ORIGINS", "https://a.example,https://b.example")

    settings = ApiSettings(_env_file=None)

    assert [k.get_secret_value() for k in settings.api_keys] == ["key-one", "key-two"]
    assert settings.cors_origins == ["https://a.example", "https://b.example"]


def test_api_keys_are_hidden_from_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_KEYS", "super-secret-key")

    assert "super-secret-key" not in repr(ApiSettings(_env_file=None))


def test_api_settings_never_read_hf_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_TOKEN", "hf_should_not_be_seen")

    settings = ApiSettings(_env_file=None)

    assert not hasattr(settings, "hf_token")
    assert "hf_should_not_be_seen" not in repr(settings)


def test_worker_requires_token_unless_fake() -> None:
    with pytest.raises(ValidationError, match="HF_TOKEN is required"):
        WorkerSettings(_env_file=None)

    assert WorkerSettings(_env_file=None, use_fake_model=True).hf_token is None


def test_blank_hf_token_counts_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_TOKEN", "  ")

    with pytest.raises(ValidationError, match="HF_TOKEN is required"):
        WorkerSettings(_env_file=None)


def test_worker_token_is_hidden_from_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_TOKEN", "hf_secret123")

    settings = WorkerSettings(_env_file=None)

    assert settings.hf_token is not None
    assert settings.hf_token.get_secret_value() == "hf_secret123"
    assert "hf_secret123" not in repr(settings)


@pytest.mark.parametrize(("raw", "expected"), [("true", True), ("false", False), ("", None)])
def test_load_in_4bit(monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool | None) -> None:
    monkeypatch.setenv("LOAD_IN_4BIT", raw)

    assert WorkerSettings(_env_file=None, use_fake_model=True).load_in_4bit is expected


def test_load_in_4bit_unset_means_auto() -> None:
    assert WorkerSettings(_env_file=None, use_fake_model=True).load_in_4bit is None


def test_heartbeat_stale_after_must_exceed_interval() -> None:
    with pytest.raises(ValidationError, match="HEARTBEAT_STALE_AFTER"):
        ApiSettings(_env_file=None, heartbeat_interval=10, heartbeat_stale_after=10)
