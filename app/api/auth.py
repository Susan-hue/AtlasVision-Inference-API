from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import Annotated, Protocol

from fastapi import Depends, Request
from fastapi.security import APIKeyHeader
from pydantic import SecretStr

from app.api.errors import ApiError
from app.core.schemas import Owner

CLIENT_ID = re.compile(r"[A-Za-z0-9_-]{8,128}")

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
client_id_header = APIKeyHeader(name="X-Client-Id", auto_error=False)


def key_id(key: str) -> str:
    """Stable, non-secret id for a key; stored on jobs and sessions instead of the key."""
    return hashlib.sha256(key.encode()).hexdigest()[:16]


class KeyStore(Protocol):
    async def lookup(self, presented: str) -> str | None:
        """Return the key id for a valid key, else None."""
        ...


class EnvKeyStore:
    def __init__(self, keys: list[SecretStr]) -> None:
        self._digests = [
            (key_id(k.get_secret_value()), hashlib.sha256(k.get_secret_value().encode()).digest())
            for k in keys
        ]

    async def lookup(self, presented: str) -> str | None:
        # Compare fixed-length digests against every key without stopping early, so timing
        # reveals neither which key matched nor how much of a guess was right.
        candidate = hashlib.sha256(presented.encode()).digest()
        match = None
        for kid, digest in self._digests:
            if hmac.compare_digest(candidate, digest):
                match = kid
        return match


@dataclass(frozen=True)
class Principal:
    key_id: str
    client_id: str | None

    @property
    def owner(self) -> Owner:
        return Owner(key_id=self.key_id, client_id=self.client_id)

    @property
    def client_scope(self) -> str:
        return f"{self.key_id}:{self.client_id or '-'}"


async def authenticate(
    request: Request,
    api_key: Annotated[str | None, Depends(api_key_header)],
    client_id: Annotated[str | None, Depends(client_id_header)],
) -> Principal:
    kid = getattr(request.state, "key_id", None)
    if kid is None and api_key:
        store: KeyStore = request.app.state.key_store
        kid = await store.lookup(api_key)
    if kid is None:
        raise ApiError("unauthorized", headers={"WWW-Authenticate": "ApiKey"})
    # X-Client-Id is only trusted once the key is valid: it comes from our own server route.
    if client_id is not None and not CLIENT_ID.fullmatch(client_id):
        raise ApiError("invalid_client_id")
    return Principal(key_id=kid, client_id=client_id)
