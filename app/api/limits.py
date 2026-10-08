from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Literal

from fastapi import Depends, Request

from app.api.auth import Principal, authenticate
from app.api.errors import ApiError
from app.core.config import ApiSettings
from app.core.ratelimit import Limit, hit

Bucket = Literal["submit", "read"]


def _limits(settings: ApiSettings, bucket: Bucket, principal: Principal) -> list[Limit]:
    if bucket == "submit":
        per_client, per_key = (
            settings.rate_limit_submit_per_client,
            settings.rate_limit_submit_per_key,
        )
    else:
        per_client, per_key = settings.rate_limit_read_per_client, settings.rate_limit_read_per_key
    return [Limit(principal.client_scope, per_client), Limit(principal.key_id, per_key)]


def rate_limited(bucket: Bucket) -> Callable[..., Awaitable[Principal]]:
    async def dependency(
        request: Request, principal: Annotated[Principal, Depends(authenticate)]
    ) -> Principal:
        settings: ApiSettings = request.app.state.settings
        retry_after = await hit(
            request.app.state.redis, bucket, _limits(settings, bucket, principal)
        )
        if retry_after is not None:
            raise ApiError.from_code("rate_limited", headers={"Retry-After": str(retry_after)})
        return principal

    return dependency


SubmitPrincipal = Annotated[Principal, Depends(rate_limited("submit"))]
ReadPrincipal = Annotated[Principal, Depends(rate_limited("read"))]
