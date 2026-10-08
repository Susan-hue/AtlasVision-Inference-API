from __future__ import annotations

import threading
import time

from testatlasvision import (
    LANGUAGES,
    AuthenticationError,
    ChatResult,
    GatedModelAccessError,
    GenerationConfig,
    ImageLoadError,
    ModelInfo,
    ModelLoadError,
    Session,
    Turn,
    UnsupportedLanguageError,
    load_image,
    normalize_language,
)
from testatlasvision.images import ImageInput

LOAD_ERRORS: dict[str, type[Exception]] = {
    cls.__name__: cls for cls in (AuthenticationError, GatedModelAccessError, ModelLoadError)
}
RAISABLE: dict[str, type[Exception]] = {
    **LOAD_ERRORS,
    **{cls.__name__: cls for cls in (ImageLoadError, UnsupportedLanguageError, ValueError)},
    "RuntimeError": RuntimeError,
}


class FakeAtlasVision:
    """Deterministic, GPU-free stand-in for TestAtlasVision.

    Put `__raise_<ErrorName>__` (a key of RAISABLE) in a question to make chat() raise it.
    """

    def __init__(
        self,
        *,
        stage: str = "2b",
        load_seconds: float = 0.0,
        generation_seconds: float = 0.0,
        load_error: str | None = None,
    ) -> None:
        time.sleep(load_seconds)
        if load_error is not None:
            raise LOAD_ERRORS[load_error](f"Fake {load_error} raised at load time")
        self._stage = stage
        self._generation_seconds = generation_seconds
        self._lock = threading.Lock()
        self.generations = 0

    def chat(
        self,
        image: ImageInput,
        question: str,
        *,
        lang: str = "en",
        session: Session | None = None,
        translate_question: bool = True,
        config: GenerationConfig | None = None,
    ) -> ChatResult:
        code = normalize_language(lang)
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question must be a non-empty string")
        for name, error in RAISABLE.items():
            if f"__raise_{name}__" in question:
                raise error(f"Fake {name}")
        picture = load_image(image, allow_remote=False)

        q_en = question.strip()
        if code != "en" and translate_question:
            self._generate()
            q_en = f"[{code}->en] {q_en}"

        self._generate()
        turn = len(session.history) + 1 if session is not None else 1
        a_en = f"Fake answer {turn} about a {picture.width}x{picture.height} image: {q_en}"
        if session is not None:
            session.history.append(Turn(q_en, a_en))

        answer = a_en
        if code != "en":
            self._generate()
            answer = f"[en->{code}] {a_en}"
        return ChatResult(answer=answer, language=code, english_question=q_en, english_answer=a_en)

    def new_session(self) -> Session:
        return Session()

    def info(self) -> ModelInfo:
        return ModelInfo(
            stage=self._stage,
            llm="fake",
            vision="fake",
            repo="fake",
            device="cpu",
            dtype="none",
            load_in_4bit=False,
            supported_languages=dict(LANGUAGES),
        )

    def close(self) -> None:
        pass

    def _generate(self) -> None:
        with self._lock:
            time.sleep(self._generation_seconds)
            self.generations += 1
