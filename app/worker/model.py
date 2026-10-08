from __future__ import annotations

from typing import Protocol

from testatlasvision import (
    AuthenticationError,
    ChatResult,
    GatedModelAccessError,
    GenerationConfig,
    ModelInfo,
    Session,
    TestAtlasVision,
)
from testatlasvision.images import ImageInput

from app.core.config import WorkerSettings
from app.worker.fake_model import FakeAtlasVision


class ChatModel(Protocol):
    def chat(
        self,
        image: ImageInput,
        question: str,
        *,
        lang: str = "en",
        session: Session | None = None,
        translate_question: bool = True,
        config: GenerationConfig | None = None,
    ) -> ChatResult: ...

    def info(self) -> ModelInfo: ...

    def close(self) -> None: ...


def load_model(settings: WorkerSettings) -> ChatModel:
    """Blocking; takes minutes for the real model. Call it via asyncio.to_thread."""
    if settings.use_fake_model:
        return FakeAtlasVision(
            stage=settings.atlas_stage,
            load_seconds=settings.fake_load_seconds,
            generation_seconds=settings.fake_generation_seconds,
            load_error=settings.fake_load_error,
        )
    assert settings.hf_token is not None
    return TestAtlasVision(
        token=settings.hf_token.get_secret_value(),
        stage=settings.atlas_stage,
        load_in_4bit=settings.load_in_4bit,
        allow_remote_images=False,
    )


def load_error_code(exc: BaseException) -> str:
    """Code published in the "failed" heartbeat. The API never returns it; it is there for
    anyone inspecting Redis (`HGETALL atlas:workers`). The worker log has the full reason."""
    if isinstance(exc, AuthenticationError):
        return "model_auth_error"
    if isinstance(exc, GatedModelAccessError):
        return "model_access_denied"
    return "model_load_error"
