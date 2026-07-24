"""Transport-neutral ownership for one realtime voice connection."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, TypeVar

from ..lifecycle import run_with_hard_deadline


class SessionClosedError(RuntimeError):
    """Raised when new playback is requested after session shutdown starts."""


class PlaybackAlreadyActiveError(RuntimeError):
    """Raised when a binding starts overlapping playback without fencing the first."""


@dataclass(frozen=True, slots=True)
class PlaybackRef:
    """Exact identity carried by every asynchronous playback callback."""

    connection_epoch: str
    generation: int

    def __post_init__(self) -> None:
        if not self.connection_epoch:
            raise ValueError("connection_epoch must not be empty")
        if self.generation <= 0:
            raise ValueError("generation must be positive")


class CancelDisposition(StrEnum):
    APPLIED = "applied"
    STALE_TARGET = "stale_target"
    SESSION_CLOSED = "session_closed"


class AsyncClosePort(Protocol):
    async def close(self) -> None: ...


CallbackResult = TypeVar("CallbackResult")


class VoiceSessionState:
    """Own playback fencing and teardown independently of a device wire format.

    A binding starts one playback at a time and attaches the returned reference to
    every producer callback. Completing or cancelling requires that same reference,
    so callbacks from a prior response or connection cannot affect the current one.
    """

    def __init__(
        self,
        *,
        close_ports: Iterable[AsyncClosePort] = (),
        connection_epoch: str | None = None,
        close_stage_timeout_seconds: float = 2.0,
    ) -> None:
        self._connection_epoch = connection_epoch or f"conn_{uuid.uuid4().hex}"
        if not self._connection_epoch:
            raise ValueError("connection_epoch must not be empty")
        if close_stage_timeout_seconds <= 0:
            raise ValueError("close_stage_timeout_seconds must be positive")
        self._close_ports = tuple(close_ports)
        self._close_stage_timeout_seconds = close_stage_timeout_seconds
        self._generation = 0
        self._active_playback: PlaybackRef | None = None
        self._closed = False
        self._transition_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None

    @property
    def connection_epoch(self) -> str:
        return self._connection_epoch

    @property
    def playback_generation(self) -> int:
        return self._generation

    @property
    def active_playback(self) -> PlaybackRef | None:
        return self._active_playback

    @property
    def closed(self) -> bool:
        return self._closed

    async def begin_playback(self) -> PlaybackRef:
        async with self._transition_lock:
            if self._closed:
                raise SessionClosedError("voice session is closed")
            if self._active_playback is not None:
                raise PlaybackAlreadyActiveError("finish or cancel the active playback before starting another")
            self._generation += 1
            playback = PlaybackRef(self._connection_epoch, self._generation)
            self._active_playback = playback
            return playback

    def accepts_callback(self, playback: PlaybackRef) -> bool:
        return not self._closed and playback == self._active_playback

    def accept_callback(self, playback: PlaybackRef, callback: Callable[[], CallbackResult]) -> CallbackResult | None:
        """Run a non-awaiting sink only while its exact playback remains active."""

        if not self.accepts_callback(playback):
            return None
        return callback()

    async def finish_playback(self, target: PlaybackRef) -> bool:
        async with self._transition_lock:
            if self._closed or target != self._active_playback:
                return False
            self._active_playback = None
            return True

    async def cancel_playback(self, target: PlaybackRef) -> CancelDisposition:
        async with self._transition_lock:
            if self._closed:
                return CancelDisposition.SESSION_CLOSED
            if target != self._active_playback:
                return CancelDisposition.STALE_TARGET

            # This transition is intentionally provider-free. The caller sends the
            # exact wire fence first and performs any slow provider interrupt later.
            self._generation += 1
            self._active_playback = None
            return CancelDisposition.APPLIED

    async def close(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._close_impl(),
                name=f"voice-session-close-{self._connection_epoch}",
            )
        await asyncio.shield(self._close_task)

    async def _close_impl(self) -> None:
        async with self._transition_lock:
            if self._closed:
                return
            self._closed = True
            if self._active_playback is not None:
                self._generation += 1
                self._active_playback = None

        errors: list[BaseException] = []
        for port in reversed(self._close_ports):
            try:
                closed = await run_with_hard_deadline(
                    port.close(),
                    timeout=self._close_stage_timeout_seconds,
                    task_name=f"voice-session-port-close-{self._connection_epoch}",
                )
                if not closed.completed:
                    errors.append(TimeoutError("voice session close port timed out"))
            except BaseException as exc:
                errors.append(exc)
        if errors:
            raise BaseExceptionGroup("voice session cleanup failed", errors)
