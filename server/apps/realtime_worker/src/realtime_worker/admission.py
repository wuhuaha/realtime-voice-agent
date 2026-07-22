from __future__ import annotations

import asyncio
import uuid


class SharedSessionAdmission:
    """One process-wide hard bound shared by every media binding."""

    def __init__(self, max_sessions: int) -> None:
        self._max_sessions = max_sessions
        self._reservations: dict[str, tuple[str, str]] = {}
        self._principals: set[tuple[str, str]] = set()
        self._lock = asyncio.Lock()
        self._draining = False

    async def reserve(self, principal: tuple[str, str]) -> str | None:
        async with self._lock:
            if self._draining or len(self._reservations) >= self._max_sessions or principal in self._principals:
                return None
            token = uuid.uuid4().hex
            self._reservations[token] = principal
            self._principals.add(principal)
            return token

    async def release(self, token: str) -> None:
        async with self._lock:
            principal = self._reservations.pop(token, None)
            if principal is not None:
                self._principals.discard(principal)

    @property
    def active_count(self) -> int:
        return len(self._reservations)

    @property
    def draining(self) -> bool:
        return self._draining

    def set_draining(self, value: bool) -> None:
        self._draining = value


__all__ = ["SharedSessionAdmission"]
