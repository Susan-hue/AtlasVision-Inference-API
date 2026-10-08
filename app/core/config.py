"""Settings, read from environment variables (and an optional `.env` file).

Each process builds only the settings class it needs:

- `SharedSettings`: values both the API and the worker must agree on (Redis, TTLs, limits).
- `ApiSettings`: API-only values (client keys, CORS, upload and rate limits).
- `WorkerSettings`: worker-only values (Hugging Face token, model loading).

The API never builds `WorkerSettings`, so `HF_TOKEN` is never read in the API process, even
if it happens to be present in its environment.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Any, Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Lists come from env vars as comma-separated strings ("a,b,c"). NoDecode stops
# pydantic-settings from trying to parse them as JSON first.
CommaList = Annotated[list[str], NoDecode]
SecretCommaList = Annotated[list[SecretStr], NoDecode]


def _split_commas(value: Any) -> Any:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


class SharedSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    redis_url: str = "redis://localhost:6379/0"

    # Lifetimes, in seconds.
    image_ttl: int = Field(default=15 * 60, gt=0)
    result_ttl: int = Field(default=60 * 60, gt=0)
    session_ttl: int = Field(default=24 * 60 * 60, gt=0)

    # Longest a single job may run before arq gives up on it.
    job_timeout: int = Field(default=300, gt=0)
    # Older turns are dropped before each call so prompts don't grow without bound.
    max_session_turns: int = Field(default=8, gt=0)

    # The worker publishes a heartbeat every `heartbeat_interval` seconds; the API treats
    # the worker as unavailable when the last one is older than `heartbeat_stale_after`.
    heartbeat_interval: int = Field(default=10, gt=0)
    heartbeat_stale_after: int = Field(default=30, gt=0)

    # Questions/answers can be personal, so they are only logged when explicitly enabled.
    log_content: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @model_validator(mode="after")
    def _heartbeat_window(self) -> Self:
        if self.heartbeat_stale_after <= self.heartbeat_interval:
            raise ValueError("HEARTBEAT_STALE_AFTER must be greater than HEARTBEAT_INTERVAL")
        return self


class ApiSettings(SharedSettings):
    # Client API keys. Kept as SecretStr so they never appear in reprs or logs.
    api_keys: SecretCommaList = []
    cors_origins: CommaList = []

    max_image_mb: float = Field(default=10, gt=0)
    # Upper bound on max_new_tokens per request. Non-English chat doubles it for the
    # back-translation step, so this also bounds the worst-case latency of a job.
    max_new_tokens: int = Field(default=256, gt=0)
    max_question_chars: int = Field(default=1000, gt=0)
    max_text_chars: int = Field(default=4000, gt=0)

    # Jobs waiting in the queue before new submissions get 503 + Retry-After.
    max_queue_depth: int = Field(default=100, gt=0)
    # Requests per minute, per API key.
    rate_limit: int = Field(default=60, gt=0)

    @field_validator("api_keys", "cors_origins", mode="before")
    @classmethod
    def _split_lists(cls, value: Any) -> Any:
        return _split_commas(value)

    @property
    def max_image_bytes(self) -> int:
        return int(self.max_image_mb * 1024 * 1024)


class WorkerSettings(SharedSettings):
    hf_token: SecretStr | None = None
    atlas_stage: Literal["1", "2", "2b"] = "2b"
    # Unset means "let the SDK decide" (4-bit on GPUs with less than 20 GB).
    load_in_4bit: bool | None = None
    use_fake_model: bool = False

    @field_validator("hf_token", "load_in_4bit", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: Any) -> Any:
        # `HF_TOKEN=` copied from .env.example must mean "no token", not an empty one.
        return None if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def _token_required_for_real_model(self) -> Self:
        if not self.use_fake_model and self.hf_token is None:
            raise ValueError("HF_TOKEN is required unless USE_FAKE_MODEL=true")
        return self


@lru_cache
def get_api_settings() -> ApiSettings:
    return ApiSettings()


@lru_cache
def get_worker_settings() -> WorkerSettings:
    return WorkerSettings()
