from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from livekit import rtc
from livekit.agents import vad as vad_api
from realtime_worker.vad import ResettingVAD

pytestmark = pytest.mark.unit


class FakeVAD:
    capabilities = vad_api.VADCapabilities(update_interval=0.032)
    model = "fake"
    provider = "fake"

    def __init__(self) -> None:
        self.last_stream: FakeVADStream | None = None

    def stream(self) -> FakeVADStream:
        self.last_stream = FakeVADStream()
        return self.last_stream


class FakeVADStream:
    def __init__(self) -> None:
        self.frames: list[rtc.AudioFrame] = []
        self.flush_count = 0
        self._events: asyncio.Queue[object] = asyncio.Queue()

    def push_frame(self, frame: rtc.AudioFrame) -> None:
        self.frames.append(frame)

    def flush(self) -> None:
        self.flush_count += 1

    def end_input(self) -> None:
        self._events.put_nowait(None)

    async def aclose(self) -> None:
        self._events.put_nowait(None)

    def emit(self, event_type: vad_api.VADEventType) -> None:
        self._events.put_nowait(SimpleNamespace(type=event_type))

    def __aiter__(self) -> FakeVADStream:
        return self

    async def __anext__(self) -> object:
        event = await self._events.get()
        if event is None:
            raise StopAsyncIteration
        return event


def audio_frame() -> rtc.AudioFrame:
    return rtc.AudioFrame(
        data=b"\0" * 640,
        sample_rate=16_000,
        num_channels=1,
        samples_per_channel=320,
    )


@pytest.mark.asyncio
async def test_vad_resets_after_idle_before_forwarding_the_next_frame() -> None:
    now = [0.0]
    delegate = FakeVAD()
    stream = ResettingVAD(  # type: ignore[arg-type]
        delegate,
        idle_reset_seconds=30.0,
        clock=lambda: now[0],
    ).stream()
    inner = delegate.last_stream
    assert inner is not None

    stream.push_frame(audio_frame())
    await asyncio.sleep(0)
    now[0] = 31.0
    stream.push_frame(audio_frame())
    await asyncio.sleep(0)

    assert inner.flush_count == 1
    assert len(inner.frames) == 2
    await stream.aclose()


@pytest.mark.asyncio
async def test_vad_defers_idle_reset_while_speaking_and_resets_after_end() -> None:
    now = [0.0]
    delegate = FakeVAD()
    stream = ResettingVAD(  # type: ignore[arg-type]
        delegate,
        idle_reset_seconds=30.0,
        clock=lambda: now[0],
    ).stream()
    inner = delegate.last_stream
    assert inner is not None

    inner.emit(vad_api.VADEventType.START_OF_SPEECH)
    await anext(stream)
    now[0] = 31.0
    stream.push_frame(audio_frame())
    await asyncio.sleep(0)
    assert inner.flush_count == 0

    inner.emit(vad_api.VADEventType.END_OF_SPEECH)
    await anext(stream)
    stream.push_frame(audio_frame())
    await asyncio.sleep(0)
    assert inner.flush_count == 1
    await stream.aclose()


@pytest.mark.asyncio
async def test_vad_close_survives_caller_cancellation_and_reuses_one_close_owner() -> None:
    class BlockingVADStream(FakeVADStream):
        def __init__(self) -> None:
            super().__init__()
            self.close_started = asyncio.Event()
            self.release_close = asyncio.Event()
            self.close_calls = 0
            self.close_finished = False

        async def aclose(self) -> None:
            self.close_calls += 1
            self.close_started.set()
            await self.release_close.wait()
            self.close_finished = True
            await super().aclose()

    class BlockingVAD(FakeVAD):
        def stream(self) -> BlockingVADStream:
            stream = BlockingVADStream()
            self.last_stream = stream
            return stream

    delegate = BlockingVAD()
    stream = ResettingVAD(  # type: ignore[arg-type]
        delegate,
        idle_reset_seconds=30.0,
    ).stream()
    inner = delegate.last_stream
    assert isinstance(inner, BlockingVADStream)

    first_close = asyncio.create_task(stream.aclose())
    await asyncio.wait_for(inner.close_started.wait(), timeout=0.1)
    first_close.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_close

    second_close = asyncio.create_task(stream.aclose())
    await asyncio.sleep(0)
    assert not second_close.done()

    inner.release_close.set()
    await asyncio.wait_for(second_close, timeout=0.1)

    assert inner.close_calls == 1
    assert inner.close_finished
    assert not any(task.get_name() == "vad-reset-input" for task in asyncio.all_tasks())


@pytest.mark.asyncio
async def test_vad_concurrent_close_serializes_non_reentrant_generator_close() -> None:
    class NonReentrantCloseProbe:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.calls = 0
            self.running = False

        async def aclose(self) -> None:
            if self.running:
                raise RuntimeError("aclose(): asynchronous generator is already running")
            self.running = True
            self.calls += 1
            self.started.set()
            await self.release.wait()
            self.running = False

    delegate = FakeVAD()
    stream = ResettingVAD(  # type: ignore[arg-type]
        delegate,
        idle_reset_seconds=30.0,
    ).stream()
    close_probe = NonReentrantCloseProbe()
    stream._tee_aiter = close_probe  # type: ignore[attr-defined]  # noqa: SLF001

    first_close = asyncio.create_task(stream.aclose())
    await asyncio.wait_for(close_probe.started.wait(), timeout=0.1)
    second_close = asyncio.create_task(stream.aclose())
    await asyncio.sleep(0)

    assert not second_close.done()
    close_probe.release.set()
    await asyncio.gather(first_close, second_close)
    assert close_probe.calls == 1
