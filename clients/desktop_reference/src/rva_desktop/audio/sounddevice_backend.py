"""Optional sounddevice backend; importing this module does not load PortAudio."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import math
import sys
from array import array
from collections import deque
from dataclasses import dataclass
from typing import Any

from .ports import (
    WIRE_FORMAT,
    WIRE_SAMPLES_PER_FRAME,
    AudioFormat,
    Clock,
    PcmFrame,
    RenderAck,
    SystemClock,
    require_wire_frame,
)
from .queue import AudioQueueClosed, BoundedAudioQueue
from .resample import Pcm16MonoResampler, PcmFramer, WirePcmConverter

_RENDER_CLOCK_POLL_SECONDS = 0.05


class SoundDeviceUnavailableError(RuntimeError):
    pass


def _load_sounddevice() -> Any:
    try:
        return importlib.import_module("sounddevice")
    except ImportError as exc:
        raise SoundDeviceUnavailableError(
            "sounddevice is required only for interactive microphone/speaker use"
        ) from exc


@dataclass(frozen=True, slots=True)
class SoundDeviceInputConfig:
    sample_rate_hz: int = 48_000
    channels: int = 1
    block_duration_ms: int = 20
    queue_capacity: int = 8
    device: int | str | None = None

    def __post_init__(self) -> None:
        if self.sample_rate_hz <= 0 or self.channels <= 0 or self.block_duration_ms <= 0:
            raise ValueError("input audio dimensions must be positive")
        if self.queue_capacity <= 0:
            raise ValueError("queue_capacity must be positive")


@dataclass(frozen=True, slots=True)
class SoundDeviceOutputConfig:
    sample_rate_hz: int = 48_000
    channels: int = 1
    queue_capacity: int = 4
    device: int | str | None = None

    def __post_init__(self) -> None:
        if self.sample_rate_hz <= 0 or self.channels <= 0:
            raise ValueError("output audio dimensions must be positive")
        if self.queue_capacity <= 0:
            raise ValueError("queue_capacity must be positive")


@dataclass(frozen=True, slots=True)
class _PendingRender:
    ack: RenderAck
    deadline: float


def _output_latency(stream: Any) -> float:
    latency = stream.latency
    if isinstance(latency, tuple):
        latency = latency[-1]
    value = float(latency)
    if not math.isfinite(value) or value < 0:
        raise RuntimeError("sounddevice reported an invalid output latency")
    return value


def _stream_time(stream: Any) -> float:
    value = float(stream.time)
    if not math.isfinite(value):
        raise RuntimeError("sounddevice reported an invalid stream time")
    return value


def _write_with_render_deadline(stream: Any, data: bytes) -> float:
    stream.write(data)
    return _stream_time(stream) + _output_latency(stream)


class SoundDeviceAudioSource:
    """Microphone source with callback isolation and drop-oldest overload policy."""

    def __init__(self, config: SoundDeviceInputConfig, *, clock: Clock | None = None) -> None:
        self._config = config
        self._clock = clock or SystemClock()
        self._raw_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=config.queue_capacity)
        self._converter = WirePcmConverter(config.sample_rate_hz, config.channels)
        self._framer = PcmFramer()
        self._ready: deque[bytes] = deque()
        self._sequence = 0
        self._stream: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started = False
        self._closed = False
        self.callback_overruns = 0
        self.device_status_events = 0

    @property
    def format(self) -> AudioFormat:
        return WIRE_FORMAT

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("audio source is closed")
        if self._started:
            return
        sounddevice = _load_sounddevice()
        self._loop = asyncio.get_running_loop()
        blocksize = self._config.sample_rate_hz * self._config.block_duration_ms // 1_000
        stream: Any = None

        def callback(indata: Any, _frames: int, _time_info: Any, status: Any) -> None:
            raw = bytes(indata)
            loop = self._loop
            if loop is None or self._closed:
                return
            if status:
                loop.call_soon_threadsafe(self._record_status)
            loop.call_soon_threadsafe(self._accept_raw, raw)

        try:
            stream = sounddevice.RawInputStream(
                samplerate=self._config.sample_rate_hz,
                blocksize=blocksize,
                device=self._config.device,
                channels=self._config.channels,
                dtype="int16",
                callback=callback,
            )
            await asyncio.to_thread(stream.start)
        except Exception as exc:
            if stream is not None:
                with contextlib.suppress(Exception):
                    stream.close()
            raise SoundDeviceUnavailableError("failed to open the configured input device") from exc
        self._stream = stream
        self._started = True

    def _record_status(self) -> None:
        self.device_status_events += 1

    def _accept_raw(self, raw: bytes) -> None:
        if self._closed:
            return
        if self._raw_queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._raw_queue.get_nowait()
            self.callback_overruns += 1
        self._raw_queue.put_nowait(raw)

    async def read_frame(self) -> PcmFrame | None:
        if not self._started:
            raise RuntimeError("audio source is not started")
        while not self._ready:
            raw = await self._raw_queue.get()
            if raw is None:
                return None
            self._ready.extend(self._framer.push(self._converter.push(raw)))
        data = self._ready.popleft()
        sequence = self._sequence
        self._sequence += 1
        return PcmFrame(
            data=data,
            sequence=sequence,
            timestamp_samples=sequence * WIRE_SAMPLES_PER_FRAME,
            captured_at=self._clock.now(),
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        stream, self._stream = self._stream, None
        if stream is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(stream.stop)
            with contextlib.suppress(Exception):
                await asyncio.to_thread(stream.close)
        while not self._raw_queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._raw_queue.get_nowait()
        self._raw_queue.put_nowait(None)


def _upmix_mono(pcm16le: bytes, channels: int) -> bytes:
    if channels == 1:
        return pcm16le
    samples = array("h")
    samples.frombytes(pcm16le)
    if sys.byteorder != "little":
        samples.byteswap()
    interleaved = array("h", (sample for sample in samples for _ in range(channels)))
    if sys.byteorder != "little":
        interleaved.byteswap()
    return interleaved.tobytes()


def _resample_wire_frame(pcm16le: bytes, sample_rate_hz: int) -> bytes:
    converter = Pcm16MonoResampler(16_000, 1, sample_rate_hz)
    return converter.push(pcm16le) + converter.flush()


class SoundDeviceAudioSink:
    """Speaker sink whose bounded queue propagates device backpressure."""

    def __init__(self, config: SoundDeviceOutputConfig) -> None:
        self._config = config
        self._queue: BoundedAudioQueue[PcmFrame] = BoundedAudioQueue(config.queue_capacity)
        self._stream: Any = None
        self._writer: asyncio.Task[None] | None = None
        self._render_monitor: asyncio.Task[None] | None = None
        self._render_queue: asyncio.Queue[_PendingRender] = asyncio.Queue(
            maxsize=config.queue_capacity
        )
        self._failure: BaseException | None = None
        self._rendered: dict[int, RenderAck] = {}
        self._submitted_frames = 0
        self._crossed_frames = 0
        self._render_condition = asyncio.Condition()
        self._started = False
        self._closed = False

    @property
    def format(self) -> AudioFormat:
        return WIRE_FORMAT

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("audio sink is closed")
        if self._started:
            return
        sounddevice = _load_sounddevice()
        stream: Any = None
        try:
            stream = sounddevice.RawOutputStream(
                samplerate=self._config.sample_rate_hz,
                device=self._config.device,
                channels=self._config.channels,
                dtype="int16",
            )
            await asyncio.to_thread(stream.start)
            await asyncio.to_thread(_output_latency, stream)
            await asyncio.to_thread(_stream_time, stream)
        except Exception as exc:
            if stream is not None:
                with contextlib.suppress(Exception):
                    stream.close()
            raise SoundDeviceUnavailableError("failed to open the configured output device") from exc
        self._stream = stream
        self._started = True
        self._render_monitor = asyncio.create_task(
            self._run_render_monitor(), name="desktop-audio-render-monitor"
        )
        self._writer = asyncio.create_task(self._run_writer(), name="desktop-audio-output")

    async def write_frame(self, frame: PcmFrame) -> None:
        if not self._started:
            raise RuntimeError("audio sink is not started")
        if self._closed:
            raise RuntimeError("audio sink is closed")
        self._raise_failure()
        require_wire_frame(frame)
        await self._queue.put(frame)
        self._raise_failure()

    async def wait_rendered(self, sequence: int) -> RenderAck:
        async with self._render_condition:
            await self._render_condition.wait_for(
                lambda: sequence in self._rendered or self._failure is not None or self._closed
            )
            self._raise_failure()
            ack = self._rendered.pop(sequence, None)
            if ack is None:
                raise RuntimeError("audio sink closed before the frame crossed the render boundary")
            return ack

    async def _run_writer(self) -> None:
        try:
            while True:
                try:
                    frame = await self._queue.get()
                except AudioQueueClosed:
                    return
                try:
                    converted = _resample_wire_frame(frame.data, self._config.sample_rate_hz)
                    if converted:
                        deadline = await asyncio.to_thread(
                            _write_with_render_deadline,
                            self._stream,
                            _upmix_mono(converted, self._config.channels),
                        )
                        pending = _PendingRender(
                            RenderAck(frame.sequence, frame.timestamp_samples), deadline
                        )
                        await self._render_queue.put(pending)
                        self._submitted_frames += 1
                finally:
                    await self._queue.task_done()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._failure = exc
            await self._queue.close(discard_pending=True)
            monitor = self._render_monitor
            if monitor is not None:
                monitor.cancel()
            async with self._render_condition:
                self._render_condition.notify_all()

    async def _run_render_monitor(self) -> None:
        try:
            while True:
                pending = await self._render_queue.get()
                try:
                    await self._wait_for_render_deadline(pending.deadline)
                    async with self._render_condition:
                        self._rendered[pending.ack.sequence] = pending.ack
                        self._crossed_frames += 1
                        self._render_condition.notify_all()
                finally:
                    self._render_queue.task_done()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._failure = exc
            await self._queue.close(discard_pending=True)
            writer = self._writer
            if writer is not None:
                writer.cancel()
            async with self._render_condition:
                self._render_condition.notify_all()

    async def _wait_for_render_deadline(self, deadline: float) -> None:
        while True:
            current_time = await asyncio.to_thread(_stream_time, self._stream)
            remaining = deadline - current_time
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, _RENDER_CLOCK_POLL_SECONDS))

    async def drain(self) -> None:
        if not self._started:
            raise RuntimeError("audio sink is not started")
        await self._queue.join()
        self._raise_failure()
        target = self._submitted_frames
        async with self._render_condition:
            await self._render_condition.wait_for(
                lambda: self._crossed_frames >= target
                or self._failure is not None
                or self._closed
            )
            self._raise_failure()
            if self._crossed_frames < target:
                raise RuntimeError("audio sink closed before crossing the render boundary")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._queue.close()
        async with self._render_condition:
            self._render_condition.notify_all()
        writer = self._writer
        if writer is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await writer
        self._writer = None
        monitor = self._render_monitor
        if monitor is not None:
            if self._failure is None:
                async with self._render_condition:
                    await self._render_condition.wait_for(
                        lambda: self._crossed_frames >= self._submitted_frames
                        or self._failure is not None
                    )
            monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor
        self._render_monitor = None
        stream, self._stream = self._stream, None
        if stream is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(stream.stop)
            with contextlib.suppress(Exception):
                await asyncio.to_thread(stream.close)

    async def abort(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._queue.close(discard_pending=True)
        async with self._render_condition:
            self._render_condition.notify_all()
        writer = self._writer
        if writer is not None:
            writer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await writer
        self._writer = None
        monitor = self._render_monitor
        if monitor is not None:
            monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor
        self._render_monitor = None
        stream, self._stream = self._stream, None
        if stream is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(stream.abort)
            with contextlib.suppress(Exception):
                await asyncio.to_thread(stream.close)

    def _raise_failure(self) -> None:
        if self._failure is not None:
            raise RuntimeError("sounddevice output worker failed") from self._failure


__all__ = [
    "SoundDeviceAudioSink",
    "SoundDeviceAudioSource",
    "SoundDeviceInputConfig",
    "SoundDeviceOutputConfig",
    "SoundDeviceUnavailableError",
]
