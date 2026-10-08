from __future__ import annotations

import inspect

import pytest
from testatlasvision import ImageLoadError, Session, TestAtlasVision

from app.worker.fake_model import RAISABLE, FakeAtlasVision
from tests.conftest import image_bytes


def _shape(method: object) -> list[tuple[str, object, object]]:
    return [(p.name, p.kind, p.default) for p in inspect.signature(method).parameters.values()]


@pytest.mark.parametrize("method", ["chat", "info", "close", "new_session"])
def test_signatures_match_sdk(method: str) -> None:
    assert _shape(getattr(FakeAtlasVision, method)) == _shape(getattr(TestAtlasVision, method))


def test_english_chat_is_one_generation() -> None:
    model = FakeAtlasVision()

    result = model.chat(image_bytes(), "What is this?", lang="en")

    assert model.generations == 1
    assert result.answer == result.english_answer
    assert result.english_question == "What is this?"
    assert result.english_answer == "Fake answer 1 about a 32x24 image: What is this?"


def test_english_question_translated_answer_is_two_generations() -> None:
    model = FakeAtlasVision()

    result = model.chat(image_bytes(), "What is this?", lang="yo", translate_question=False)

    assert model.generations == 2
    assert result.english_question == "What is this?"
    assert result.answer == f"[en->yo] {result.english_answer}"
    assert result.language == "yo"


def test_non_english_question_is_three_generations() -> None:
    model = FakeAtlasVision()

    result = model.chat(image_bytes(), "Kini eyi?", lang="Hausa", translate_question=True)

    assert model.generations == 3
    assert result.english_question == "[ha->en] Kini eyi?"
    assert result.language == "ha"


def test_session_stores_english_turns() -> None:
    model, session = FakeAtlasVision(), Session()

    model.chat(image_bytes(), "First?", lang="ig", session=session, translate_question=False)
    second = model.chat(
        image_bytes(), "Second?", lang="ig", session=session, translate_question=False
    )

    assert [t.question for t in session.history] == ["First?", "Second?"]
    assert second.english_answer.startswith("Fake answer 2 ")


@pytest.mark.parametrize("image", [b"not an image", "https://example.com/cat.png"])
def test_rejects_bad_bytes_and_urls(image: object) -> None:
    with pytest.raises(ImageLoadError):
        FakeAtlasVision().chat(image, "What is this?")  # type: ignore[arg-type]


@pytest.mark.parametrize(("name", "error"), RAISABLE.items())
def test_raises_on_demand(name: str, error: type[Exception]) -> None:
    with pytest.raises(error):
        FakeAtlasVision().chat(image_bytes(), f"__raise_{name}__")


@pytest.mark.parametrize("name", ["AuthenticationError", "GatedModelAccessError", "ModelLoadError"])
def test_load_errors(name: str) -> None:
    with pytest.raises(RAISABLE[name]):
        FakeAtlasVision(load_error=name)
