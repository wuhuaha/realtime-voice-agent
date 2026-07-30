from __future__ import annotations

import asyncio

import pytest

from rva_desktop.audio import (
    WIRE_BYTES_PER_FRAME,
    WIRE_FRAME_DURATION_MS,
    WIRE_SAMPLES_PER_FRAME,
    AudioSink,
    AudioSource,
    FixturePcmSource,
    NullAudioSink,
    PcmFrame,
    RecordingAudioSink,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = 10.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.value

    async def sleep(self, delay_seconds: float) -> None:
        self.sleeps.append(delay_seconds)
        self.value += delay_seconds


def test_pcm_frame_enforces_wire_size_and_non_negative_timeline() -> None:
    with pytest.raises(ValueError, match="exactly"):
        PcmFrame(data=b"short", sequence=0, timestamp_samples=0, captured_at=0.0)
    with pytest.raises(ValueError, match="sequence"):
        PcmFrame(
            data=b"\x00" * WIRE_BYTES_PER_FRAME,
            sequence=-1,
            timestamp_samples=0,
            captured_at=0.0,
        )


def test_fixture_source_is_deterministic_and_implements_port() -> None:
    async def scenario() -> None:
        clock = FakeClock()
        pcm = bytes([1]) * WIRE_BYTES_PER_FRAME + bytes([2]) * WIRE_BYTES_PER_FRAME
        source = FixturePcmSource(pcm, clock=clock, paced=True)
        assert isinstance(source, AudioSource)

        await source.start()
        first = await source.read_frame()
        second = await source.read_frame()
        end = await source.read_frame()

        assert first is not None and first.sequence == 0 and first.timestamp_samples == 0
        assert second is not None and second.sequence == 1
        assert second.timestamp_samples == WIRE_SAMPLES_PER_FRAME
        assert first.captured_at == 10.0
        assert second.captured_at == 10.0 + WIRE_FRAME_DURATION_MS / 1_000
        assert clock.sleeps == [WIRE_FRAME_DURATION_MS / 1_000]
        assert end is None
        await source.close()

    asyncio.run(scenario())


def test_fixture_rejects_partial_frame_unless_padding_is_explicit() -> None:
    with pytest.raises(ValueError, match="complete"):
        FixturePcmSource(b"\x01\x02")
    source = FixturePcmSource(b"\x01\x02", pad_final_frame=True)

    async def scenario() -> None:
        await source.start()
        frame = await source.read_frame()
        assert frame is not None
        assert frame.data[:2] == b"\x01\x02"
        assert frame.data[2:] == b"\x00" * (WIRE_BYTES_PER_FRAME - 2)

    asyncio.run(scenario())


def test_recording_and_null_sinks_support_headless_e2e() -> None:
    async def scenario() -> None:
        frame = PcmFrame(
            data=b"\x34\x12" * WIRE_SAMPLES_PER_FRAME,
            sequence=3,
            timestamp_samples=3 * WIRE_SAMPLES_PER_FRAME,
            captured_at=12.5,
        )
        recording = RecordingAudioSink(max_frames=1)
        null = NullAudioSink()
        assert isinstance(recording, AudioSink)
        assert isinstance(null, AudioSink)

        await recording.start()
        await recording.write_frame(frame)
        await recording.drain()
        recording_ack = await recording.wait_rendered(frame.sequence)
        assert recording_ack.sequence == frame.sequence
        assert recording_ack.timestamp_samples == frame.timestamp_samples
        assert recording_ack.rendered_samples == WIRE_SAMPLES_PER_FRAME
        assert recording.frames == (frame,)
        assert recording.pcm == frame.data
        with pytest.raises(BufferError):
            await recording.write_frame(frame)
        await recording.close()

        await null.start()
        null_frame = PcmFrame(
            data=frame.data,
            sequence=0,
            timestamp_samples=0,
            captured_at=frame.captured_at,
        )
        await null.write_frame(null_frame)
        await null.drain()
        null_ack = await null.wait_rendered(null_frame.sequence)
        assert null_ack.sequence == null_frame.sequence
        assert null_ack.timestamp_samples == null_frame.timestamp_samples
        assert null.frames_written == 1
        assert null.bytes_written == WIRE_BYTES_PER_FRAME
        with pytest.raises(RuntimeError, match="boundary"):
            await null.wait_rendered(1)
        await null.close()

    asyncio.run(scenario())
