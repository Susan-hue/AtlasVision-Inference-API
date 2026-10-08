from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError
from starlette.exceptions import HTTPException
from testatlasvision import AtlasVisionError

from app.core.errors import ERRORS, code_for

logger = logging.getLogger(__name__)

HTTP_CODES = {400: "bad_request", 404: "not_found", 405: "method_not_allowed"}


class ApiError(Exception):
    """An error response; status and message come from the catalog in app/core/errors.py."""

    def __init__(
        self,
        code: str,
        *,
        headers: dict[str, str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.headers = headers
        self.extra = extra or {}


def error_response(
    code: str, headers: dict[str, str] | None = None, extra: dict[str, Any] | None = None
) -> JSONResponse:
    status, message = ERRORS[code]
    if status is None:
        raise ValueError(f"{code} is only reported inside failed jobs")
    body = {"error": {"code": code, "message": message, **(extra or {})}}
    return JSONResponse(body, status_code=status, headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return error_response(exc.code, exc.headers, exc.extra)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0]
        field = ".".join(str(part) for part in first["loc"] if part != "body")
        return error_response("invalid_request", extra={"field": field, "problem": first["msg"]})

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException) -> JSONResponse:
        code = HTTP_CODES.get(exc.status_code)
        if code is None:
            logger.error("Unexpected HTTP %s: %s", exc.status_code, exc.detail)
            code = "internal_error"
        return error_response(code, exc.headers)

    @app.exception_handler(AtlasVisionError)
    async def _sdk_error(request: Request, exc: AtlasVisionError) -> JSONResponse:
        logger.warning("SDK error in API: %s", type(exc).__name__, exc_info=True)
        return error_response(code_for(exc))

    @app.exception_handler(RedisError)
    async def _redis_error(request: Request, exc: RedisError) -> JSONResponse:
        logger.error("Redis error: %s", type(exc).__name__)
        retry_after = request.app.state.settings.retry_after_service_unavailable
        return error_response("service_unavailable", {"Retry-After": str(retry_after)})

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.error("Unhandled error: %s", type(exc).__name__, exc_info=exc)
        return error_response("internal_error")
