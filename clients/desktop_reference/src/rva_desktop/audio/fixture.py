"""Deterministic audio sources and sinks used by E2E and unit tests."""

from __future__ import annotations

from collections.abc import Iterable

from .ports import (
    WIRE_BYTES_PER_FRAME,
    WIRE_FORMAT,
    WIRE_FRAME_DURATION_MS,
    WIRE_SAMPLES_PER_FRAME,
    AudioFormat,
    Clock,
    PcmFrame,
    RenderAck,
    SystemClock,
    require_wire_frame,
)


class FixturePcmSource:
    """Finite, repeatable PCM source; it never accesses a host audio device."""

    def __init__(
        self,
        pcm16le: bytes,
        *,
        clock: Clock | None = None,
        paced: bool = False,
        pad_final_frame: bool = False,
    ) -> None:
        remainder = len(pcm16le) % WIRE_BYTES_PER_FRAME
        if remainder:
            if not pad_final_frame:
                raise ValueError("fixture PCM must contain complete 60 ms wire frames")
            pcm16le += b"\x00" * (WIRE_BYTES_PER_FRAME - remainder)
        self._frames = tuple(
            pcm16le[offset : offset + WIRE_BYTES_PER_FRAME]
            for offset in range(0, len(pcm16le), WIRE_BYTES_PER_FRAME)
        )
        self._clock = clock or SystemClock()
        self._paced = paced
        self._index = 0
        self._started = False
        self._closed = False

    @classmethod
    def from_frames(
        cls,
        frames: Iterable[bytes],
        *,
        clock: Clock | None = None,
        paced: bool = False,
    ) -> FixturePcmSource:
        return cls(b"".join(frames), clock=clock, paced=paced)

    @property
    def format(self) -> AudioFormat:
        return WIRE_FORMAT

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("audio source is closed")
        self._started = True

    async def read_frame(self) -> PcmFrame | None:
        if not self._started:
            raise RuntimeError("audio source is not started")
        if self._closed or self._index >= len(self._frames):
            return None
        if self._paced and self._index:
            await self._clock.sleep(WIRE_FRAME_DURATION_MS / 1_000)
        sequence = self._index
        frame = PcmFrame(
            data=self._frames[sequence],
            sequence=sequence,
            timestamp_samples=sequence * WIRE_SAMPLES_PER_FRAME,
            captured_at=self._clock.now(),
        )
        self._index += 1
        return frame

    async def close(self) -> None:
        self._closed = True


class RecordingAudioSink:
    """In-memory sink for deterministic playback assertions."""

    def __init__(self, *, max_frames: int | None = None) -> None:
        if max_frames is not None and max_frames <= 0:
            raise ValueError("max_frames must be positive")
        self._max_frames = max_frames
        self._frames: list[PcmFrame] = []
        self._rendered: dict[int, RenderAck] = {}
        self._started = False
        self._closed = False

    @property
    def format(self) -> AudioFormat:
        return WIRE_FORMAT

    @property
    def frames(self) -> tuple[PcmFrame, ...]:
        return tuple(self._frames)

    @property
    def pcm(self) -> bytes:
        return b"".join(frame.data for frame in self._frames)

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("audio sink is closed")
        self._started = True

    async def write_frame(self, frame: PcmFrame) -> None:
        if not self._started:
            raise RuntimeError("audio sink is not started")
        if self._closed:
            raise RuntimeError("audio sink is closed")
        require_wire_frame(frame)
        if self._max_frames is not None and len(self._frames) >= self._max_frames:
            raise BufferError("recording sink capacity exceeded")
        self._frames.append(frame)
        self._rendered[frame.sequence] = RenderAck(frame.sequence, frame.timestamp_samples)

    async def wait_rendered(self, sequence: int) -> RenderAck:
        ack = self._rendered.pop(sequence, None)
        if ack is None:
            raise RuntimeError("frame has not crossed the recording sink boundary")
        return ack

    async def drain(self) -> None:
        if not self._started:
            raise RuntimeError("audio sink is not started")

    async def close(self) -> None:
        self._closed = True


class NullAudioSink:
    """Playback sink for headless E2E runs that retains only counters."""

    def __init__(self) -> None:
        self.frames_written = 0
        self.bytes_written = 0
        self._rendered: dict[int, RenderAck] = {}
        self._started = False
        self._closed = False

    @property
    def format(self) -> AudioFormat:
        return WIRE_FORMAT

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("audio sink is closed")
        self._started = True

    async def write_frame(self, frame: PcmFrame) -> None:
        if not self._started:
            raise RuntimeError("audio sink is not started")
        if self._closed:
            raise RuntimeError("audio sink is closed")
        require_wire_frame(frame)
        self.frames_written += 1
        self.bytes_written += len(frame.data)
        self._rendered[frame.sequence] = RenderAck(frame.sequence, frame.timestamp_samples)

    async def wait_rendered(self, sequence: int) -> RenderAck:
        ack = self._rendered.pop(sequence, None)
        if ack is None:
            raise RuntimeError("frame has not crossed the null sink boundary")
        return ack

    async def drain(self) -> None:
        if not self._started:
            raise RuntimeError("audio sink is not started")

    async def close(self) -> None:
        self._closed = True


__all__ = ["FixturePcmSource", "NullAudioSink", "RecordingAudioSink"]
