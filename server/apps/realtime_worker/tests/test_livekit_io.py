from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from livekit import rtc
from livekit.agents import StopResponse
from pydantic import ValidationError
from realtime_worker.agent import (
    AgentRunnerTerminal,
    AgentRunnerTerminalKind,
    AgentRunnerTerminatedError,
    LiveKitAgentRunner,
    RoomlessAudioInput,
    RoomlessAudioOutput,
    _DefaultAgent,
)
from realtime_worker.audio import PcmFrame
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
    output.on_playback_started(created_at=1.0)
    output.on_playback_finished(playback_position=0.02, interrupted=False)
    result = await output.wait_for_playout()
    assert len(segments) == 1 and segments[0].producer_epoch == 1 and len(segments[0].frames) == 1
    assert result.playback_position == 0.02 and not result.interrupted
    await output.close()
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
async def test_output_limit_chunks_a_long_response_without_stalling_generation() -> None:
    segments = []
    response_ends = []

    async def emit(segment):  # noqa: ANN001
        segments.append(segment)

    output = RoomlessAudioOutput(emit, response_ends.append, max_segment_frames=2)
    frame = rtc.AudioFrame(data=b"\x00" * 640, sample_rate=16_000, num_channels=1, samples_per_channel=320)
    await output.capture_frame(frame)
    await output.capture_frame(frame)
    await output.capture_frame(frame)

    output.flush()
    output.on_playback_started(created_at=1.0)
    output.on_playback_finished(playback_position=0.06, interrupted=False)
    result = await output.wait_for_playout()
    await output.close()
    assert [len(segment.frames) for segment in segments] == [2, 1]
    assert [segment.producer_epoch for segment in segments] == [1, 1]
    assert [frame.sequence for segment in segments for frame in segment.frames] == [0, 1, 2]
    assert response_ends == [1]
    assert result.playback_position == 0.06 and not result.interrupted


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


@pytest.mark.asyncio
async def test_output_close_releases_pending_wait_for_playout() -> None:
    response_ends = []

    async def emit(_segment):  # noqa: ANN001
        return None

    output = RoomlessAudioOutput(emit, response_ends.append)
    frame = rtc.AudioFrame(data=b"\x00" * 640, sample_rate=16_000, num_channels=1, samples_per_channel=320)
    await output.capture_frame(frame)
    output.flush()

    waiter = asyncio.create_task(
        output.wait_for_playout(),
        name="RoomlessAudioOutput.wait_for_playout",
    )
    await asyncio.sleep(0)
    assert not waiter.done()

    await output.close()
    result = await asyncio.wait_for(waiter, timeout=0.1)

    assert result.interrupted
    assert response_ends == [1]
    assert not any(task.get_name() == "RoomlessAudioOutput.wait_for_playout" for task in asyncio.all_tasks())


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
async def test_custom_input_short_burst_waits_for_capacity_and_propagates_cancellation() -> None:
    audio_input = RoomlessAudioInput(1, queue_timeout_seconds=0.2)
    first = PcmFrame(0, 0, 0, b"\x00" * 640)
    second = PcmFrame(0, 1, 320, b"\x00" * 640)
    await audio_input.push(first)

    pending = asyncio.create_task(audio_input.push(second))
    await asyncio.sleep(0.1)
    assert not pending.done()
    await audio_input.__anext__()
    await asyncio.wait_for(pending, timeout=0.1)
    await audio_input.__anext__()

    await audio_input.push(first)
    cancelled = asyncio.create_task(audio_input.push(second))
    await asyncio.sleep(0)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    audio_input.close()


@pytest.mark.asyncio
async def test_custom_input_close_wakes_waiting_producer_without_queueing_after_sentinel() -> None:
    audio_input = RoomlessAudioInput(1, queue_timeout_seconds=0.1)
    frame = PcmFrame(0, 0, 0, b"\x00" * 640)
    await audio_input.push(frame)
    pending = asyncio.create_task(audio_input.push(frame))
    await asyncio.sleep(0)

    audio_input.close()
    await asyncio.wait_for(pending, timeout=0.1)

    with pytest.raises(StopAsyncIteration):
        await audio_input.__anext__()
    assert audio_input._queue.empty()  # noqa: SLF001


@pytest.mark.asyncio
async def test_custom_input_runtime_terminal_rejects_blocked_and_future_producers() -> None:
    audio_input = RoomlessAudioInput(1, queue_timeout_seconds=0.1)
    frame = PcmFrame(0, 0, 0, b"\x00" * 640)
    await audio_input.push(frame)
    pending = asyncio.create_task(audio_input.push(frame))
    await asyncio.sleep(0)
    terminal = AgentRunnerTerminal(AgentRunnerTerminalKind.RUNTIME_FAILED)

    audio_input.close(terminal)

    with pytest.raises(AgentRunnerTerminatedError):
        await asyncio.wait_for(pending, timeout=0.1)
    with pytest.raises(AgentRunnerTerminatedError):
        await audio_input.push(frame)
    with pytest.raises(StopAsyncIteration):
        await audio_input.__anext__()
    assert audio_input._queue.empty()  # noqa: SLF001


@pytest.mark.asyncio
async def test_custom_input_sustained_block_reports_bounded_queue_snapshot() -> None:
    audio_input = RoomlessAudioInput(1, queue_timeout_seconds=0.02)
    frame = PcmFrame(0, 0, 0, b"\x00" * 640)
    await audio_input.push(frame)

    with pytest.raises(BufferError, match="LiveKit input queue is full") as captured:
        await audio_input.push(frame)

    assert captured.value.source == "pcm_input"  # type: ignore[attr-defined]
    assert captured.value.qsize == 1  # type: ignore[attr-defined]
    assert captured.value.capacity == 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_default_agent_uses_message_bound_suppression() -> None:
    seen: list[object] = []
    agent = _DefaultAgent(lambda message: seen.append(message) is None and True)
    message = SimpleNamespace(raw_text_content="echo")

    with pytest.raises(StopResponse):
        await agent.on_user_turn_completed(object(), message)
    assert seen == [message]


def _runner() -> LiveKitAgentRunner:
    async def emit(_segment):  # noqa: ANN001
        return None

    settings = Settings(
        _env_file=None,
        runner="livekit",
        deepseek_api_key="test-key",
    )
    return LiveKitAgentRunner(settings, emit, lambda _epoch: None)


def test_livekit_runner_suppresses_pre_ack_playback_turn_exactly_once() -> None:
    runner = _runner()
    runner.set_text_sinks(lambda _text, _final: None, lambda _text: None)
    runner.set_response_gate(True)
    runner._handle_user_state("listening", "speaking")  # noqa: SLF001
    runner._handle_user_transcript("echo", True)  # noqa: SLF001

    message = SimpleNamespace(raw_text_content="echo")
    assert runner._response_gate.should_suppress(message) is True  # noqa: SLF001
    runner.set_response_gate(False)
    assert runner._response_gate.should_suppress(message) is False  # noqa: SLF001


def test_livekit_runner_suppresses_turn_that_ends_after_playback() -> None:
    runner = _runner()
    runner.set_response_gate(True)
    runner._handle_user_state("listening", "speaking")  # noqa: SLF001
    runner.set_response_gate(False)
    runner._handle_user_state("speaking", "listening")  # noqa: SLF001
    runner._handle_user_transcript("late echo", True)  # noqa: SLF001

    assert runner._response_gate.should_suppress(  # noqa: SLF001
        SimpleNamespace(raw_text_content="late echo")
    ) is True


def test_livekit_runner_skipped_hook_does_not_poison_next_clean_turn() -> None:
    runner = _runner()
    runner.set_response_gate(True)
    runner._handle_user_state("listening", "speaking")  # noqa: SLF001
    runner._handle_user_transcript("same words", True)  # noqa: SLF001
    runner.set_response_gate(False)

    # Simulate LiveKit skipping on_user_turn_completed for the contaminated
    # turn, then starting a genuinely clean turn with identical text.
    runner._handle_user_state("listening", "speaking")  # noqa: SLF001
    runner._handle_user_transcript("same words", True)  # noqa: SLF001

    assert runner._response_gate.should_suppress(  # noqa: SLF001
        SimpleNamespace(raw_text_content="same words")
    ) is False


def test_livekit_runner_multiple_overlap_turns_are_not_collapsed_to_one_bit() -> None:
    runner = _runner()
    runner.set_response_gate(True)
    for text in ("first", "second"):
        runner._handle_user_state("listening", "speaking")  # noqa: SLF001
        runner._handle_user_transcript(text, True)  # noqa: SLF001

    runner.set_response_gate(False)
    assert runner._response_gate.should_suppress(SimpleNamespace(raw_text_content="first")) is True  # noqa: SLF001
    assert runner._response_gate.should_suppress(SimpleNamespace(raw_text_content="second")) is True  # noqa: SLF001
