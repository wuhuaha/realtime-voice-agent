from __future__ import annotations

import asyncio

import pytest
from livekit import rtc
from livekit.agents import StopResponse
from pydantic import ValidationError
from realtime_worker.agent import LiveKitAgentRunner, RoomlessAudioInput, RoomlessAudioOutput, _DefaultAgent
from realtime_worker.config import Settings

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_public_custom_io_tracks_segment_and_physical_completion() -> None:
    segments = []
    stop_calls = []

    async def emit(frames):  # noqa: ANN001
        segments.append(frames)

    output = RoomlessAudioOutput(emit, lambda _epoch: stop_calls.append(True))
    frame = rtc.AudioFrame(data=b"\x00" * 640, sample_rate=16_000, num_channels=1, samples_per_channel=320)
    await output.capture_frame(frame)
    output.flush()
    await output.close()
    assert len(segments) == 1 and segments[0].producer_epoch == 1 and len(segments[0].frames) == 1
    output.on_playback_started(created_at=1.0)
    output.on_playback_finished(playback_position=0.02, interrupted=False)
    result = await output.wait_for_playout()
    assert result.playback_position == 0.02 and not result.interrupted
    output.clear_buffer()
    assert stop_calls == [True]


@pytest.mark.asyncio
async def test_output_clear_buffer_only_discards_pending_pcm_without_stopping_active_playback() -> None:
    segments = []
    stop_calls = []

    async def emit(segment):  # noqa: ANN001
        segments.append(segment)

    output = RoomlessAudioOutput(emit, lambda epoch: stop_calls.append(epoch))
    frame = rtc.AudioFrame(data=b"\x00" * 640, sample_rate=16_000, num_channels=1, samples_per_channel=320)

    await output.capture_frame(frame)
    output.clear_buffer()
    output.flush()
    await output.capture_frame(frame)
    output.flush()
    await output.close()

    assert stop_calls == []
    assert len(segments) == 1
    assert segments[0].producer_epoch == 2
    assert len(segments[0].frames) == 1


@pytest.mark.asyncio
async def test_output_accepts_a_normal_segment_longer_than_five_seconds() -> None:
    segments = []

    async def emit(segment):  # noqa: ANN001
        segments.append(segment)

    output = RoomlessAudioOutput(emit, lambda _epoch: None)
    frame = rtc.AudioFrame(data=b"\x00" * 640, sample_rate=16_000, num_channels=1, samples_per_channel=320)
    for _ in range(300):
        await output.capture_frame(frame)

    output.flush()
    await output.close()

    assert len(segments) == 1
    assert len(segments[0].frames) == 300


@pytest.mark.asyncio
async def test_output_allows_multiple_flushes_while_previous_segment_is_pending() -> None:
    release = asyncio.Event()
    segments = []

    async def emit(segment):  # noqa: ANN001
        segments.append(segment)
        await release.wait()

    output = RoomlessAudioOutput(emit, lambda _epoch: None)
    frame = rtc.AudioFrame(data=b"\x00" * 640, sample_rate=16_000, num_channels=1, samples_per_channel=320)

    await output.capture_frame(frame)
    output.flush()
    await output.capture_frame(frame)
    output.flush()

    await asyncio.sleep(0)
    assert len(segments) == 2
    release.set()
    await output.close()


@pytest.mark.asyncio
async def test_output_preserves_pcm_across_non_aligned_livekit_callbacks() -> None:
    segments = []

    async def emit(segment):  # noqa: ANN001
        segments.append(segment)

    output = RoomlessAudioOutput(emit, lambda _epoch: None)
    first = rtc.AudioFrame(
        data=b"\x11" * 320,
        sample_rate=16_000,
        num_channels=1,
        samples_per_channel=160,
    )
    second = rtc.AudioFrame(
        data=b"\x22" * 960,
        sample_rate=16_000,
        num_channels=1,
        samples_per_channel=480,
    )

    await output.capture_frame(first)
    await output.capture_frame(second)
    output.flush()
    await output.close()

    assert len(segments) == 1
    assert [frame.pcm for frame in segments[0].frames] == [
        b"\x11" * 320 + b"\x22" * 320,
        b"\x22" * 640,
    ]


@pytest.mark.asyncio
async def test_output_pads_only_the_final_partial_pcm_frame() -> None:
    segments = []

    async def emit(segment):  # noqa: ANN001
        segments.append(segment)

    output = RoomlessAudioOutput(emit, lambda _epoch: None)
    frame = rtc.AudioFrame(
        data=b"\x33" * 320,
        sample_rate=16_000,
        num_channels=1,
        samples_per_channel=160,
    )

    await output.capture_frame(frame)
    output.flush()
    await output.close()

    assert len(segments) == 1
    assert segments[0].frames[0].pcm == b"\x33" * 320 + b"\x00" * 320


@pytest.mark.asyncio
async def test_output_limit_rejects_the_whole_segment_with_duration_diagnostics() -> None:
    segments = []

    async def emit(segment):  # noqa: ANN001
        segments.append(segment)

    output = RoomlessAudioOutput(emit, lambda _epoch: None, max_segment_frames=2)
    frame = rtc.AudioFrame(data=b"\x00" * 640, sample_rate=16_000, num_channels=1, samples_per_channel=320)
    await output.capture_frame(frame)
    await output.capture_frame(frame)

    with pytest.raises(
        BufferError,
        match=r"received_frames=3 .*limit_frames=2 .*received_seconds=0\.060 .*limit_seconds=0\.040",
    ):
        await output.capture_frame(frame)

    output.flush()
    await output.close()
    assert segments == []


@pytest.mark.asyncio
async def test_output_interrupt_fences_partial_segment_and_advances_epoch() -> None:
    segments = []

    async def emit(segment):  # noqa: ANN001
        segments.append(segment)

    output = RoomlessAudioOutput(emit, lambda _epoch: None)
    frame = rtc.AudioFrame(data=b"\x00" * 640, sample_rate=16_000, num_channels=1, samples_per_channel=320)
    await output.capture_frame(frame)
    assert output.advance_producer_epoch() == 2
    output.flush()
    await output.capture_frame(frame)
    output.flush()
    await output.close()

    assert len(segments) == 1
    assert segments[0].producer_epoch == 2
    assert len(segments[0].frames) == 1


@pytest.mark.asyncio
async def test_output_close_discards_an_unflushed_segment() -> None:
    segments = []

    async def emit(segment):  # noqa: ANN001
        segments.append(segment)

    output = RoomlessAudioOutput(emit, lambda _epoch: None)
    frame = rtc.AudioFrame(data=b"\x00" * 640, sample_rate=16_000, num_channels=1, samples_per_channel=320)
    await output.capture_frame(frame)

    await output.close()
    output.flush()

    assert segments == []


def test_output_segment_limit_is_configured_in_seconds_and_converted_to_pcm_frames() -> None:
    settings = Settings(_env_file=None, output_segment_max_seconds=30)

    assert settings.output_segment_max_frames == 1_500


def test_livekit_runner_applies_the_configured_output_segment_limit() -> None:
    async def emit(_segment):  # noqa: ANN001
        return None

    settings = Settings(
        _env_file=None,
        runner="livekit",
        deepseek_api_key="test-key",
        output_segment_max_seconds=12,
    )
    runner = LiveKitAgentRunner(settings, emit, lambda _epoch: None)

    assert runner._output._max_segment_frames == 600  # noqa: SLF001


@pytest.mark.parametrize("seconds", [4, 121])
def test_output_segment_limit_rejects_unsafe_configuration(seconds: int) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, output_segment_max_seconds=seconds)


@pytest.mark.asyncio
async def test_custom_input_is_bounded_and_closes_iterator() -> None:
    audio_input = RoomlessAudioInput(1)
    audio_input.close()
    with pytest.raises(StopAsyncIteration):
        await audio_input.__anext__()


@pytest.mark.asyncio
async def test_default_agent_drops_each_marked_overlap_turn_exactly_once() -> None:
    drops = iter((True, False))
    agent = _DefaultAgent(lambda: next(drops))

    with pytest.raises(StopResponse):
        await agent.on_user_turn_completed(object(), object())
    await agent.on_user_turn_completed(object(), object())


def test_livekit_runner_marks_next_turn_from_runtime_transcript_callback() -> None:
    async def emit(_segment):  # noqa: ANN001
        return None

    settings = Settings(
        _env_file=None,
        runner="livekit",
        deepseek_api_key="test-key",
    )
    runner = LiveKitAgentRunner(settings, emit, lambda _epoch: None)
    runner.set_text_sinks(lambda _text, _final: True, lambda _text: None)

    runner._handle_user_transcript("overlap", True)  # noqa: SLF001

    assert runner._consume_overlap_turn() is True  # noqa: SLF001
    assert runner._consume_overlap_turn() is False  # noqa: SLF001
