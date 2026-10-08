from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.errors import error_response
from app.core.errors import ERRORS

if TYPE_CHECKING:
    from app.api.auth import KeyStore

OPEN_PATHS = frozenset({"/health"})


class BodySizeLimit:
    """Only read a request body for valid key holders, and never more than `max_bytes`.

    The API key is checked before any body is read, so callers without a valid key cannot
    make the API take in an upload. A declared Content-Length over the limit is rejected
    unread; otherwise the body is read up to the limit and replayed to the app. Without
    this, the multipart parser would spool an upload of any size before any route ran.
    """

    def __init__(self, app: ASGIApp, max_bytes: int, key_store: KeyStore) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.key_store = key_store

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        if scope["path"] not in OPEN_PATHS:
            presented = headers.get("x-api-key")
            kid = await self.key_store.lookup(presented) if presented else None
            if kid is None:
                await self._send_error(
                    scope, receive, send, "unauthorized", {"WWW-Authenticate": "ApiKey"}
                )
                return
            # Lets the auth dependency reuse this lookup instead of repeating it.
            scope.setdefault("state", {})["key_id"] = kid

        declared = headers.get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > self.max_bytes):
            await self._send_error(scope, receive, send, "request_too_large")
            return

        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.max_bytes:
                await self._send_error(scope, receive, send, "request_too_large")
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break

        replay: list[Message] = [{"type": "http.request", "body": b"".join(chunks)}]

        async def replay_receive() -> Message:
            return replay.pop() if replay else await receive()

        await self.app(scope, replay_receive, send)

    async def _send_error(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        code: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        spec = ERRORS[code]
        await error_response(spec.status, code, spec.message, headers)(scope, receive, send)
