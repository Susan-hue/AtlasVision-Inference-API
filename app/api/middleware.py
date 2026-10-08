from __future__ import annotations

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.errors import error_response
from app.core.errors import ERRORS


class BodySizeLimit:
    """Reject request bodies over `max_bytes` without reading more than that.

    A declared Content-Length over the limit is rejected before any body is read; otherwise
    the body is read up to the limit and replayed to the app. Without this, the multipart
    parser would spool an upload of any size before the route could look at it.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = Headers(scope=scope).get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > self.max_bytes):
            await self._reject(scope, receive, send)
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
                await self._reject(scope, receive, send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break

        replay: list[Message] = [{"type": "http.request", "body": b"".join(chunks)}]

        async def replay_receive() -> Message:
            return replay.pop() if replay else await receive()

        await self.app(scope, replay_receive, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        spec = ERRORS["request_too_large"]
        response = error_response(spec.status, "request_too_large", spec.message)
        await response(scope, receive, send)
