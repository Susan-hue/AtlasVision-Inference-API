"""Client-facing error codes. Messages here are the only error text clients ever see."""

from __future__ import annotations

from typing import NamedTuple

from testatlasvision import (
    AuthenticationError,
    GatedModelAccessError,
    ImageLoadError,
    ModelLoadError,
    UnsupportedLanguageError,
)

from app.core.schemas import JobError


class ErrorSpec(NamedTuple):
    status: int
    message: str


ERRORS: dict[str, ErrorSpec] = {
    "invalid_image": ErrorSpec(422, "The image could not be read."),
    "unsupported_language": ErrorSpec(422, "Language must be one of en, ig, yo, ha."),
    "invalid_input": ErrorSpec(422, "The request could not be processed."),
    "model_unavailable": ErrorSpec(503, "The model is not available. Try again later."),
    "internal_error": ErrorSpec(500, "Something went wrong. Please try again."),
    "job_not_found": ErrorSpec(404, "Job not found or expired."),
    "session_not_found": ErrorSpec(404, "Conversation not found or expired."),
    "session_busy": ErrorSpec(
        409,
        "This conversation is still answering your previous question. "
        "Please wait for that answer before asking another.",
    ),
    "image_expired": ErrorSpec(410, "The uploaded image expired before the job ran."),
    "job_expired": ErrorSpec(410, "The job waited too long in the queue."),
    "job_interrupted": ErrorSpec(500, "The job was interrupted."),
    "worker_lost": ErrorSpec(500, "The worker stopped before finishing the job."),
}

# Order matters: ImageLoadError and UnsupportedLanguageError are also ValueErrors.
_SDK_CODES: tuple[tuple[type[BaseException], str], ...] = (
    (ImageLoadError, "invalid_image"),
    (UnsupportedLanguageError, "unsupported_language"),
    (AuthenticationError, "model_unavailable"),
    (GatedModelAccessError, "model_unavailable"),
    (ModelLoadError, "model_unavailable"),
    (ValueError, "invalid_input"),
)


def code_for(exc: BaseException) -> str:
    for error_type, code in _SDK_CODES:
        if isinstance(exc, error_type):
            return code
    return "internal_error"


def job_error(code: str) -> JobError:
    return JobError(code=code, message=ERRORS[code].message)
