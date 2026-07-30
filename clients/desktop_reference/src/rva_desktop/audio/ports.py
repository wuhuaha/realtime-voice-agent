"""Transport-neutral audio contracts for the desktop reference client."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

WIRE_SAMPLE_RATE_HZ = 16_000
WIRE_CHANNELS = 1
WIRE_SAMPLE_WIDTH_BYTES = 2
WIRE_FRAME_DURATION_MS = 60
WIRE_SAMPLES_PER_FRAME = WIRE_SAMPLE_RATE_HZ * WIRE_FRAME_DURATION_MS // 1_000
WIRE_BYTES_PER_FRAME = WIRE_SAMPLES_PER_FRAME * WIRE_CHANNELS * WIRE_SAMPLE_WIDTH_BYTES


@dataclass(frozen=True, slots=True)
class AudioFormat:
    sample_rate_hz: int
    channels: int
    sample_width_bytes: int = WIRE_SAMPLE_WIDTH_BYTES

    def __post_init__(self) -> None:
        if self.sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be positive")
        if self.channels <= 0:
            raise ValueError("channels must be positive")
        if self.sample_width_bytes != WIRE_SAMPLE_WIDTH_BYTES:
            raise ValueError("only signed PCM16LE is supported")


WIRE_FORMAT = AudioFormat(WIRE_SAMPLE_RATE_HZ, WIRE_CHANNELS)


@dataclass(frozen=True, slots=True)
class PcmFrame:
    """One exact wire-sized PCM16LE frame."""

    data: bytes
    sequence: int
    timestamp_samples: int
    captured_at: float

    def __post_init__(self) -> None:
        if len(self.data) != WIRE_BYTES_PER_FRAME:
            raise ValueError(f"PCM frame must contain exactly {WIRE_BYTES_PER_FRAME} bytes")
        if self.sequence < 0:
            raise ValueError("sequence must be non-negative")
        if self.timestamp_samples < 0:
            raise ValueError("timestamp_samples must be non-negative")


@dataclass(frozen=True, slots=True)
class RenderAck:
    """A frame has crossed the sink's closest observable render boundary."""

    sequence: int
    timestamp_samples: int
    rendered_samples: int = WIRE_SAMPLES_PER_FRAME

    def __post_init__(self) -> None:
        if self.sequence < 0 or self.timestamp_samples < 0:
            raise ValueError("render cursor must be non-negative")
        if self.rendered_samples != WIRE_SAMPLES_PER_FRAME:
            raise ValueError("render acknowledgement must cover one wire frame")


@runtime_checkable
class Clock(Protocol):
    def now(self) -> float: ...

    async def sleep(self, delay_seconds: float) -> None: ...


class SystemClock:
    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, delay_seconds: float) -> None:
        await asyncio.sleep(delay_seconds)


@runtime_checkable
class AudioSource(Protocol):
    @property
    def format(self) -> AudioFormat: ...

    async def start(self) -> None: ...

    async def read_frame(self) -> PcmFrame | None: ...

    async def close(self) -> None: ...


@runtime_checkable
class AudioSink(Protocol):
    @property
    def format(self) -> AudioFormat: ...

    async def start(self) -> None: ...

    async def write_frame(self, frame: PcmFrame) -> None: ...

    async def wait_rendered(self, sequence: int) -> RenderAck: ...

    async def drain(self) -> None: ...

    async def close(self) -> None: ...


def require_wire_frame(frame: PcmFrame) -> None:
    # PcmFrame validates at construction; this named boundary keeps adapters explicit.
    if len(frame.data) != WIRE_BYTES_PER_FRAME:
        raise ValueError("audio adapter received a non-wire-sized frame")


__all__ = [
    "AudioFormat",
    "AudioSink",
    "AudioSource",
    "Clock",
    "PcmFrame",
    "RenderAck",
    "SystemClock",
    "WIRE_BYTES_PER_FRAME",
    "WIRE_CHANNELS",
    "WIRE_FORMAT",
    "WIRE_FRAME_DURATION_MS",
    "WIRE_SAMPLE_RATE_HZ",
    "WIRE_SAMPLES_PER_FRAME",
    "require_wire_frame",
]
