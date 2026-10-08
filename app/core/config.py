"""Settings per process. The API never builds WorkerSettings, so it never reads HF_TOKEN."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Any, Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# NoDecode: parse "a,b,c" ourselves instead of pydantic-settings' default JSON decoding.
CommaList = Annotated[list[str], NoDecode]
SecretCommaList = Annotated[list[SecretStr], NoDecode]


def _split_commas(value: Any) -> Any:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


class SharedSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    redis_url: str = "redis://localhost:6379/0"

    image_ttl: int = Field(default=15 * 60, gt=0)
    result_ttl: int = Field(default=60 * 60, gt=0)
    session_ttl: int = Field(default=24 * 60 * 60, gt=0)

    job_timeout: int = Field(default=300, gt=0)
    max_session_turns: int = Field(default=8, gt=0)

    heartbeat_interval: int = Field(default=10, gt=0)
    heartbeat_stale_after: int = Field(default=30, gt=0)

    log_content: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @model_validator(mode="after")
    def _heartbeat_window(self) -> Self:
        if self.heartbeat_stale_after <= self.heartbeat_interval:
            raise ValueError("HEARTBEAT_STALE_AFTER must be greater than HEARTBEAT_INTERVAL")
        return self


class ApiSettings(SharedSettings):
    api_keys: SecretCommaList = []
    cors_origins: CommaList = []

    max_image_mb: float = Field(default=4, gt=0)
    max_new_tokens: int = Field(default=256, gt=0)
    max_question_chars: int = Field(default=1000, gt=0)

    max_queue_depth: int = Field(default=100, gt=0)

    # Requests per minute. "Submit" is POST /v1/jobs/chat; "read" is every other endpoint.
    rate_limit_submit_per_client: int = Field(default=10, gt=0)
    rate_limit_submit_per_key: int = Field(default=120, gt=0)
    rate_limit_read_per_client: int = Field(default=120, gt=0)
    rate_limit_read_per_key: int = Field(default=3000, gt=0)

    @field_validator("api_keys", "cors_origins", mode="before")
    @classmethod
    def _split_lists(cls, value: Any) -> Any:
        return _split_commas(value)

    @property
    def max_image_bytes(self) -> int:
        return int(self.max_image_mb * 1024 * 1024)

    @property
    def max_request_bytes(self) -> int:
        # Room for the multipart framing and the text fields around the image.
        return self.max_image_bytes + 64 * 1024


class WorkerSettings(SharedSettings):
    hf_token: SecretStr | None = None
    atlas_stage: Literal["1", "2", "2b"] = "2b"
    # None lets the SDK decide (4-bit below 20 GB of GPU memory).
    load_in_4bit: bool | None = None

    use_fake_model: bool = False
    fake_load_seconds: float = Field(default=2.0, ge=0)
    fake_generation_seconds: float = Field(default=0.5, ge=0)
    fake_load_error: (
        Literal["AuthenticationError", "GatedModelAccessError", "ModelLoadError"] | None
    ) = None

    @field_validator("hf_token", "load_in_4bit", "fake_load_error", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: Any) -> Any:
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
