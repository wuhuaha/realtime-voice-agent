from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable

from pydantic import ValidationError

from .models import ConnectGrantClaims


class GrantError(ValueError):
    pass


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, UnicodeEncodeError) as exc:
        raise GrantError("malformed grant encoding") from exc


class GrantCodec:
    """Small, versioned HMAC token shared by Director and Worker."""

    def __init__(
        self,
        key: str | bytes,
        *,
        clock: Callable[[], float] = time.time,
        clock_skew_seconds: float = 2.0,
    ) -> None:
        self._key = key.encode("utf-8") if isinstance(key, str) else key
        if len(self._key) < 16:
            raise ValueError("grant signing key must contain at least 16 bytes")
        self._clock = clock
        self._clock_skew_seconds = clock_skew_seconds

    def issue(self, claims: ConnectGrantClaims) -> str:
        header = _encode(b'{"alg":"HS256","typ":"VAG","v":1}')
        payload = _encode(claims.model_dump_json(exclude_none=True).encode("utf-8"))
        signing_input = f"{header}.{payload}".encode("ascii")
        signature = _encode(hmac.new(self._key, signing_input, hashlib.sha256).digest())
        return f"{header}.{payload}.{signature}"

    def verify(
        self,
        token: str,
        *,
        worker_id: str | None = None,
        device_id: str | None = None,
    ) -> ConnectGrantClaims:
        parts = token.split(".")
        if len(parts) != 3:
            raise GrantError("malformed grant")
        header, payload, supplied_signature = parts
        try:
            header_value = json.loads(_decode(header))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise GrantError("malformed grant header") from exc
        if header_value != {"alg": "HS256", "typ": "VAG", "v": 1}:
            raise GrantError("unsupported grant header")
        signing_input = f"{header}.{payload}".encode("ascii")
        expected = hmac.new(self._key, signing_input, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _decode(supplied_signature)):
            raise GrantError("invalid grant signature")
        try:
            claims = ConnectGrantClaims.model_validate_json(_decode(payload))
        except (ValidationError, UnicodeDecodeError) as exc:
            raise GrantError("invalid grant claims") from exc
        now = self._clock()
        if claims.exp <= now:
            raise GrantError("grant expired")
        if claims.iat > now + self._clock_skew_seconds or claims.exp <= claims.iat:
            raise GrantError("invalid grant lifetime")
        if worker_id is not None and claims.worker_id != worker_id:
            raise GrantError("grant belongs to another worker")
        if device_id is not None and claims.device_id != device_id:
            raise GrantError("grant belongs to another device")
        return claims
