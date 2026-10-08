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

HTTP_CODES = {404: "not_found", 405: "method_not_allowed"}


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        headers: dict[str, str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers
        self.extra = extra or {}

    @classmethod
    def from_code(cls, code: str, **kwargs: Any) -> ApiError:
        spec = ERRORS[code]
        return cls(spec.status, code, spec.message, **kwargs)


def error_response(
    status_code: int,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    body = {"error": {"code": code, "message": message, **(extra or {})}}
    return JSONResponse(body, status_code=status_code, headers=headers)


def _from_code(code: str) -> JSONResponse:
    spec = ERRORS[code]
    return error_response(spec.status, code, spec.message)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return error_response(exc.status_code, exc.code, exc.message, exc.headers, exc.extra)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0]
        field = ".".join(str(part) for part in first["loc"] if part != "body")
        return error_response(422, "invalid_request", f"{field}: {first['msg']}")

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException) -> JSONResponse:
        code = HTTP_CODES.get(exc.status_code, "http_error")
        return error_response(exc.status_code, code, str(exc.detail), exc.headers)

    @app.exception_handler(AtlasVisionError)
    async def _sdk_error(request: Request, exc: AtlasVisionError) -> JSONResponse:
        logger.warning("SDK error in API: %s", type(exc).__name__, exc_info=True)
        return _from_code(code_for(exc))

    @app.exception_handler(RedisError)
    async def _redis_error(request: Request, exc: RedisError) -> JSONResponse:
        logger.error("Redis error: %s", type(exc).__name__)
        return error_response(
            503,
            "service_unavailable",
            "A backing service is unavailable. Try again shortly.",
            {"Retry-After": "5"},
        )

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.error("Unhandled error: %s", type(exc).__name__, exc_info=exc)
        return _from_code("internal_error")
