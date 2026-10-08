"""Client-facing error codes. Messages here are the only error text clients ever see.

Details that vary (which field, which limit) go in extra fields of the error body, never in
the message.
"""

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
    # HTTP status when sent as a response; None for codes that only appear inside a failed
    # job, which always arrives in a 200 poll response.
    status: int | None
    message: str


ERRORS: dict[str, ErrorSpec] = {
    # Problems with the request
    "bad_request": ErrorSpec(400, "The request body could not be parsed."),
    "invalid_client_id": ErrorSpec(
        400, "X-Client-Id must be 8-128 characters: letters, digits, '-' or '_'."
    ),
    "unauthorized": ErrorSpec(401, "A valid API key is required."),
    "not_found": ErrorSpec(404, "Not found."),
    "method_not_allowed": ErrorSpec(405, "Method not allowed."),
    "request_too_large": ErrorSpec(413, "The request body is too large."),
    "unsupported_media_type": ErrorSpec(415, "Image must be JPEG, PNG or WebP."),
    "invalid_request": ErrorSpec(422, "A field is missing or invalid."),
    "invalid_question": ErrorSpec(422, "question must not be empty."),
    "question_too_long": ErrorSpec(422, "question is too long."),
    "invalid_question_lang": ErrorSpec(422, 'question_lang must be "en" or equal to lang.'),
    "unsupported_language": ErrorSpec(422, "Language must be one of en, ig, yo, ha."),
    "invalid_image": ErrorSpec(422, "The image could not be read."),
    "invalid_input": ErrorSpec(None, "The request could not be processed."),
    # Sessions and jobs
    "job_not_found": ErrorSpec(404, "Job not found or expired."),
    "session_not_found": ErrorSpec(404, "Session not found or expired."),
    "session_busy": ErrorSpec(
        409,
        "This session is still answering your previous question. "
        "Please wait for that answer before asking another.",
    ),
    "image_expired": ErrorSpec(None, "The uploaded image expired before the job ran."),
    "job_expired": ErrorSpec(None, "The job waited too long in the queue."),
    "job_interrupted": ErrorSpec(None, "The job was interrupted."),
    "worker_lost": ErrorSpec(None, "The worker stopped before finishing the job."),
    # Availability and limits
    "rate_limited": ErrorSpec(429, "Too many requests. Please slow down and try again shortly."),
    "internal_error": ErrorSpec(500, "Something went wrong. Please try again."),
    "model_unavailable": ErrorSpec(503, "The model is not available. Try again later."),
    "queue_full": ErrorSpec(503, "The service is busy. Please try again shortly."),
    "service_unavailable": ErrorSpec(503, "A backing service is unavailable. Try again shortly."),
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
