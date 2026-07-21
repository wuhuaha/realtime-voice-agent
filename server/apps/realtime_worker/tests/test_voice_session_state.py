from __future__ import annotations

import asyncio

import pytest
from realtime_worker.voice.session import (
    CancelDisposition,
    PlaybackAlreadyActiveError,
    PlaybackRef,
    SessionClosedError,
    VoiceSessionState,
)


class _InterruptPort:
    def __init__(self) -> None:
        self.targets: list[PlaybackRef] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block = False

    async def interrupt(self, target: PlaybackRef) -> None:
        self.targets.append(target)
        self.started.set()
        if self.block:
            await self.release.wait()


class _ClosePort:
    def __init__(self, events: list[str], name: str, *, block: bool = False) -> None:
        self._events = events
        self._name = name
        self._block = block
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def close(self) -> None:
        self.calls += 1
        self._events.append(self._name)
        self.started.set()
        if self._block:
            await self.release.wait()


class _FailingClosePort(_ClosePort):
    async def close(self) -> None:
        await super().close()
        raise RuntimeError(f"{self._name} failed")  # noqa: SLF001


@pytest.mark.unit
async def test_each_session_gets_a_fresh_connection_epoch() -> None:
    first = VoiceSessionState(_InterruptPort())
    second = VoiceSessionState(_InterruptPort())

    assert first.connection_epoch.startswith("conn_")
    assert second.connection_epoch.startswith("conn_")
    assert first.connection_epoch != second.connection_epoch


@pytest.mark.unit
async def test_playback_generations_are_monotonic_and_never_overlap() -> None:
    session = VoiceSessionState(_InterruptPort(), connection_epoch="conn_test")

    first = await session.begin_playback()
    with pytest.raises(PlaybackAlreadyActiveError):
        await session.begin_playback()

    assert await session.finish_playback(first) is True
    second = await session.begin_playback()
    assert second.generation > first.generation
    assert session.playback_generation == second.generation


@pytest.mark.unit
async def test_cancel_requires_the_exact_connection_and_playback_generation() -> None:
    interrupt = _InterruptPort()
    session = VoiceSessionState(interrupt, connection_epoch="conn_current")
    current = await session.begin_playback()

    assert (
        await session.cancel_playback(PlaybackRef("conn_previous", current.generation))
        is CancelDisposition.STALE_TARGET
    )
    assert (
        await session.cancel_playback(PlaybackRef(current.connection_epoch, current.generation + 1))
        is CancelDisposition.STALE_TARGET
    )
    assert interrupt.targets == []
    assert session.active_playback == current

    assert await session.cancel_playback(current) is CancelDisposition.APPLIED
    assert await session.cancel_playback(current) is CancelDisposition.STALE_TARGET
    assert interrupt.targets == [current]
    assert session.active_playback is None
    assert session.playback_generation > current.generation


@pytest.mark.unit
async def test_cancel_fences_stale_callbacks_before_interrupt_completes() -> None:
    interrupt = _InterruptPort()
    interrupt.block = True
    session = VoiceSessionState(interrupt, connection_epoch="conn_current")
    old_playback = await session.begin_playback()
    accepted: list[str] = []

    cancel = asyncio.create_task(session.cancel_playback(old_playback))
    await interrupt.started.wait()

    assert session.accept_callback(old_playback, lambda: accepted.append("stale")) is None
    next_playback = asyncio.create_task(session.begin_playback())
    await asyncio.sleep(0)
    assert not next_playback.done()

    interrupt.release.set()
    assert await cancel is CancelDisposition.APPLIED
    current = await next_playback
    assert current.generation > old_playback.generation
    session.accept_callback(current, lambda: accepted.append("current"))
    assert accepted == ["current"]


@pytest.mark.unit
async def test_finish_rejects_late_callbacks_and_stale_completion() -> None:
    session = VoiceSessionState(_InterruptPort(), connection_epoch="conn_current")
    first = await session.begin_playback()

    assert await session.finish_playback(first) is True
    assert await session.finish_playback(first) is False
    assert session.accepts_callback(first) is False

    second = await session.begin_playback()
    assert await session.finish_playback(first) is False
    assert session.active_playback == second


@pytest.mark.unit
async def test_close_is_idempotent_and_caller_cancellation_does_not_cancel_cleanup() -> None:
    events: list[str] = []
    first = _ClosePort(events, "first")
    last = _ClosePort(events, "last", block=True)
    session = VoiceSessionState(_InterruptPort(), close_ports=(first, last), connection_epoch="conn_current")
    playback = await session.begin_playback()

    cancelled_waiter = asyncio.create_task(session.close())
    await last.started.wait()
    second_waiter = asyncio.create_task(session.close())
    cancelled_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_waiter

    assert session.closed is True
    assert session.accepts_callback(playback) is False
    last.release.set()
    await second_waiter
    await session.close()

    assert events == ["last", "first"]
    assert last.calls == 1
    assert first.calls == 1
    assert await session.cancel_playback(playback) is CancelDisposition.SESSION_CLOSED
    with pytest.raises(SessionClosedError):
        await session.begin_playback()


@pytest.mark.unit
async def test_close_attempts_every_owned_resource_and_reports_failures() -> None:
    events: list[str] = []
    first = _ClosePort(events, "first")
    failing = _FailingClosePort(events, "failing")
    session = VoiceSessionState(_InterruptPort(), close_ports=(first, failing), connection_epoch="conn_current")

    with pytest.raises(BaseExceptionGroup, match="voice session cleanup failed") as captured:
        await session.close()

    assert events == ["failing", "first"]
    assert len(captured.value.exceptions) == 1
    assert isinstance(captured.value.exceptions[0], RuntimeError)
    assert session.closed is True
