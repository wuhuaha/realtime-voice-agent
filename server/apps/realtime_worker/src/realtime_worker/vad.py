"""VAD lifecycle adapters for long-lived roomless audio sessions."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from livekit.agents import vad as vad_api

from .lifecycle import retain_shutdown_task


class ResettingVAD(vad_api.VAD):
    """Reset recurrent VAD state at safe boundaries in long-lived sessions."""

    def __init__(
        self,
        delegate: vad_api.VAD,
        *,
        idle_reset_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if idle_reset_seconds <= 0:
            raise ValueError("idle_reset_seconds must be positive")
        super().__init__(capabilities=delegate.capabilities)
        self._delegate = delegate
        self._idle_reset_seconds = idle_reset_seconds
        self._clock = clock

    @property
    def model(self) -> str:
        return self._delegate.model

    @property
    def provider(self) -> str:
        return self._delegate.provider

    def stream(self) -> vad_api.VADStream:
        return _ResettingVADStream(
            self,
            self._delegate.stream(),
            idle_reset_seconds=self._idle_reset_seconds,
            clock=self._clock,
        )


class _ResettingVADStream(vad_api.VADStream):
    def __init__(
        self,
        vad: ResettingVAD,
        delegate: vad_api.VADStream,
        *,
        idle_reset_seconds: float,
        clock: Callable[[], float],
    ) -> None:
        self._delegate = delegate
        self._idle_reset_seconds = idle_reset_seconds
        self._clock = clock
        self._last_reset_at = clock()
        self._speaking = False
        self._reset_pending = False
        self._close_task: asyncio.Task[None] | None = None
        super().__init__(vad)

    async def aclose(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                super().aclose(),
                name="vad-reset-close",
            )
            self._close_task.add_done_callback(_consume_task_result)
        try:
            await asyncio.shield(self._close_task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                retain_shutdown_task(
                    self._close_task,
                    task_name="vad-reset-close",
                    cancel_requested=False,
                )
            raise

    async def _main_task(self) -> None:
        forward_task = asyncio.create_task(self._forward_input(), name="vad-reset-input")
        try:
            async for event in self._delegate:
                if event.type == vad_api.VADEventType.START_OF_SPEECH:
                    self._speaking = True
                elif event.type == vad_api.VADEventType.END_OF_SPEECH:
                    self._speaking = False
                    self._reset_pending = True
                self._event_ch.send_nowait(event)
        finally:
            forward_task.cancel()
            await asyncio.gather(forward_task, return_exceptions=True)
            await self._delegate.aclose()

    async def _forward_input(self) -> None:
        async for item in self._input_ch:
            if isinstance(item, self._FlushSentinel):
                self._reset_delegate()
                continue

            now = self._clock()
            if self._reset_pending or (
                not self._speaking and now - self._last_reset_at >= self._idle_reset_seconds
            ):
                self._reset_delegate()
            self._delegate.push_frame(item)
        self._delegate.end_input()

    def _reset_delegate(self) -> None:
        self._delegate.flush()
        self._last_reset_at = self._clock()
        self._reset_pending = False


def _consume_task_result(task: asyncio.Task[None]) -> None:
    if not task.cancelled():
        task.exception()
