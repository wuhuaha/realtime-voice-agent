from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

_SECRET_FIELDS = {
    "authorization",
    "bootstrap_token",
    "connect_grant",
    "downlink_key_b64",
    "downlink_salt_b64",
    "token",
    "uplink_key_b64",
    "uplink_salt_b64",
}


class TraceSink(Protocol):
    def emit(self, name: str, fields: Mapping[str, object]) -> None: ...


@dataclass(slots=True)
class LoggingTrace:
    logger: logging.Logger = logging.getLogger("rva_desktop")

    def emit(self, name: str, fields: Mapping[str, object]) -> None:
        safe = redact(fields)
        values = " ".join(f"{key}={value!r}" for key, value in sorted(safe.items()))
        self.logger.info("event=%s %s", name, values)


class NullTrace:
    def emit(self, name: str, fields: Mapping[str, object]) -> None:
        return None


def redact(value: Any, *, field: str | None = None) -> Any:
    if field is not None and field.lower() in _SECRET_FIELDS:
        return "[redacted]"
    if isinstance(value, Mapping):
        return {str(key): redact(item, field=str(key)) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [redact(item) for item in value]
    if isinstance(value, bytes):
        return {"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()[:12]}
    return value
