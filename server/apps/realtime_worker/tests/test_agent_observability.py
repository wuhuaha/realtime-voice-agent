from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from realtime_worker import agent as agent_module
from realtime_worker.agent import LiveKitAgentRunner, RoomlessTextOutput, _register_livekit_observers
from realtime_worker.config import Settings
from realtime_worker.lifecycle import detached_shutdown_task_count
from realtime_worker.observability.events import (
    BoundedJsonLogTraceSink,
    InMemoryTraceSink,
    TraceContext,
    Tracer,
)

pytestmark = pytest.mark.unit


class FakeSession:
    def __init__(self, **options: object) -> None:
        self.options = options
        self.handlers: dict[str, object] = {}
        self.input = SimpleNamespace(audio=None)
        self.output = SimpleNamespace(audio=None)
        self.started = False
        self.closed = False

    def on(self, name: str, handler: object) -> None:
        self.handlers[name] = handler

    def emit(self, name: str, event: object) -> None:
        handler = self.handlers[name]
        handler(event)  # type: ignore[operator]

    async def start(self, _agent: object) -> None:
        self.started = True

    async def aclose(self) -> None:
        self.closed = True


class LLMMetrics:
    speech_id = "speech-1"
    request_id = "request-1"
    ttft = 0.125
    duration = 0.5


def test_public_session_observers_emit_required_pii_free_phases() -> None:
    sink = InMemoryTraceSink()
    tracer = Tracer(TraceContext(trace_id="trace-test"), sink)
    session = FakeSession()
    observer = _register_livekit_observers(session, tracer)

    session.emit("user_state_changed", SimpleNamespace(new_state="speaking", old_state="listening", created_at=1.0))
    session.emit(
        "user_input_transcribed",
        SimpleNamespace(transcript="SECRET TRANSCRIPT MUST NOT BE LOGGED", is_final=False),
    )
    session.emit(
        "user_input_transcribed",
        SimpleNamespace(transcript="SECRET TRANSCRIPT MUST NOT BE LOGGED", is_final=True),
    )
    session.emit("user_state_changed", SimpleNamespace(new_state="listening", old_state="speaking", created_at=1.5))
    session.emit("conversation_item_added", SimpleNamespace(item=SimpleNamespace(role="user")))
    session.emit("agent_state_changed", SimpleNamespace(new_state="thinking"))
    session.emit(
        "speech_created",
        SimpleNamespace(speech_handle=SimpleNamespace(id="speech-1"), source="llm", user_initiated=True),
    )
    session.emit("metrics_collected", SimpleNamespace(metrics=LLMMetrics()))
    session.emit("agent_state_changed", SimpleNamespace(new_state="speaking"))
    assert "agent_audio_published" not in [event.name for event in sink.events]
    session.emit("conversation_item_added", SimpleNamespace(item=SimpleNamespace(role="assistant")))
    observer.playback_started(2.0)
    observer.playback_finished(0.5, False)
    session.emit("close", SimpleNamespace(reason=SimpleNamespace(value="shutdown"), error=None))

    names = [event.name for event in sink.events]
    for required in (
        "user_speech_started",
        "asr_interim",
        "asr_first_interim",
        "asr_final",
        "user_speech_ended",
        "eot_committed",
        "llm_requested",
        "agent_audio_published",
        "endpoint_playback_started",
        "endpoint_playback_finished",
        "provider_metrics",
        "turn_latency_summary",
        "session_closed",
    ):
        assert required in names
    serialized = json.dumps([event.to_dict() for event in sink.events])
    assert "SECRET TRANSCRIPT" not in serialized


def test_public_session_observers_forward_real_user_transcript_only() -> None:
    sink = InMemoryTraceSink()
    tracer = Tracer(TraceContext(trace_id="trace-wire-text"), sink)
    session = FakeSession()
    transcripts: list[tuple[str, bool]] = []
    _register_livekit_observers(
        session,
        tracer,
        user_transcript=lambda text, is_final: transcripts.append((text, is_final)),
    )

    session.emit("user_input_transcribed", SimpleNamespace(transcript="你好", is_final=False))
    session.emit("user_input_transcribed", SimpleNamespace(transcript="你好世界", is_final=True))
    assert transcripts == [("你好", False), ("你好世界", True)]


@pytest.mark.asyncio
async def test_public_text_output_streams_cumulative_text_before_late_conversation_item() -> None:
    assistant_texts: list[str] = []
    output = RoomlessTextOutput(assistant_texts.append)
    session = FakeSession()
    _register_livekit_observers(
        session,
        Tracer(TraceContext(trace_id="trace-text-order"), InMemoryTraceSink()),
    )

    await output.capture_text("你好")
    await output.capture_text("呀")
    session.emit(
        "conversation_item_added",
        SimpleNamespace(item=SimpleNamespace(role="assistant", text_content="迟到文本", interrupted=False)),
    )

    assert assistant_texts == ["你好", "你好呀"]
    output.flush()
    await output.capture_text("下一句")
    assert assistant_texts[-1] == "下一句"


def test_multiple_vad_segments_reuse_semantic_turn_and_refresh_speech_milestones() -> None:
    sink = InMemoryTraceSink()
    ticks = iter(range(1, 100))
    tracer = Tracer(TraceContext(trace_id="trace-multi-vad"), sink, monotonic_ns_fn=lambda: next(ticks))
    session = FakeSession()
    _register_livekit_observers(session, tracer)

    session.emit("user_state_changed", SimpleNamespace(new_state="speaking", old_state="listening", created_at=1.0))
    first_turn = tracer.current_turn_id
    assert first_turn is not None
    first_started = tracer._turn_milestones[first_turn]["user_speech_started"]  # noqa: SLF001
    session.emit("user_state_changed", SimpleNamespace(new_state="listening", old_state="speaking", created_at=1.2))
    session.emit("user_input_transcribed", SimpleNamespace(transcript="first", is_final=True))
    session.emit("user_state_changed", SimpleNamespace(new_state="speaking", old_state="listening", created_at=1.3))
    session.emit("user_state_changed", SimpleNamespace(new_state="listening", old_state="speaking", created_at=1.5))
    session.emit("user_input_transcribed", SimpleNamespace(transcript="second", is_final=True))

    assert tracer.current_turn_id == first_turn
    assert tracer._turn_milestones[first_turn]["user_speech_started"] > first_started  # noqa: SLF001
    speech_turns = {
        event.fields["turn_id"]
        for event in sink.events
        if event.name in {"user_speech_started", "user_speech_ended", "asr_final"}
    }
    assert speech_turns == {first_turn}


def test_multi_vad_during_barge_in_reuses_new_turn_without_reassigning_old_playback_ack() -> None:
    sink = InMemoryTraceSink()
    tracer = Tracer(TraceContext(trace_id="trace-barge"), sink)
    session = FakeSession()
    observer = _register_livekit_observers(session, tracer)

    session.emit("user_state_changed", SimpleNamespace(new_state="speaking", old_state="listening", created_at=1.0))
    old_turn = tracer.current_turn_id
    session.emit("agent_state_changed", SimpleNamespace(new_state="speaking"))
    assert "agent_audio_published" not in [event.name for event in sink.events]
    session.emit("user_state_changed", SimpleNamespace(new_state="speaking", old_state="listening", created_at=1.1))
    barge_turn = tracer.current_turn_id
    assert old_turn is not None and barge_turn is not None and barge_turn != old_turn
    session.emit("user_state_changed", SimpleNamespace(new_state="listening", old_state="speaking", created_at=1.2))
    session.emit("user_state_changed", SimpleNamespace(new_state="speaking", old_state="listening", created_at=1.3))
    assert tracer.current_turn_id == barge_turn

    observer.playback_started(1.4)
    started = next(event for event in sink.events if event.name == "endpoint_playback_started")
    assert started.fields["turn_id"] == old_turn
    observer.playback_finished(0.2, True)
    assert tracer.current_turn_id == barge_turn
    assert old_turn not in tracer._turn_milestones  # noqa: SLF001


def test_multi_vad_during_thinking_barge_reuses_new_turn_and_old_response_playback() -> None:
    sink = InMemoryTraceSink()
    tracer = Tracer(TraceContext(trace_id="trace-thinking-barge"), sink)
    session = FakeSession()
    observer = _register_livekit_observers(session, tracer)

    session.emit("user_state_changed", SimpleNamespace(new_state="speaking", old_state="listening", created_at=1.0))
    response_turn = tracer.current_turn_id
    session.emit("agent_state_changed", SimpleNamespace(new_state="thinking"))
    session.emit("user_state_changed", SimpleNamespace(new_state="speaking", old_state="listening", created_at=1.1))
    barge_turn = tracer.current_turn_id
    assert response_turn is not None and barge_turn is not None and barge_turn != response_turn
    session.emit("user_state_changed", SimpleNamespace(new_state="listening", old_state="speaking", created_at=1.2))
    session.emit("user_state_changed", SimpleNamespace(new_state="speaking", old_state="listening", created_at=1.3))
    assert tracer.current_turn_id == barge_turn

    session.emit("agent_state_changed", SimpleNamespace(new_state="speaking"))
    observer.playback_started(1.4)
    started = next(event for event in sink.events if event.name == "endpoint_playback_started")
    assert started.fields["turn_id"] == response_turn


def test_assistant_item_after_playback_completion_does_not_create_pseudo_turn() -> None:
    sink = InMemoryTraceSink()
    tracer = Tracer(TraceContext(trace_id="trace-assistant-late"), sink)
    session = FakeSession()
    observer = _register_livekit_observers(session, tracer)

    session.emit("user_state_changed", SimpleNamespace(new_state="speaking", old_state="listening", created_at=1.0))
    response_turn = tracer.current_turn_id
    session.emit("agent_state_changed", SimpleNamespace(new_state="thinking"))
    session.emit("agent_state_changed", SimpleNamespace(new_state="speaking"))
    observer.playback_started(1.1)
    observer.playback_finished(0.2, False)
    assert tracer.current_turn_id is None
    session.emit("conversation_item_added", SimpleNamespace(item=SimpleNamespace(role="assistant")))

    assert tracer.current_turn_id is None
    assistant = next(event for event in sink.events if event.name == "assistant_item_committed")
    assert assistant.fields["turn_id"] == response_turn


def test_turn_summary_omits_negative_latency_and_marks_invalid_pair() -> None:
    sink = InMemoryTraceSink()
    ticks = iter((200, 100, 300))
    tracer = Tracer(
        TraceContext(trace_id="trace-negative"),
        sink,
        monotonic_ns_fn=lambda: next(ticks),
    )
    turn_id = tracer.begin_turn()
    tracer.event("user_speech_started", turn_id=turn_id)
    tracer.event("user_speech_ended", turn_id=turn_id)
    tracer.emit_turn_summary(turn_id)

    summary = sink.events[-1]
    assert summary.name == "turn_latency_summary"
    assert summary.fields["status"] == "incomplete"
    assert "user_speech_started->user_speech_ended" in str(summary.fields["invalid_stages"])
    assert "speech_duration_ms" not in summary.fields


def test_json_trace_sink_is_bounded_per_session() -> None:
    memory = InMemoryTraceSink()
    tracer = Tracer(
        TraceContext(trace_id="trace-bounded"),
        BoundedJsonLogTraceSink(max_events=2, sink=memory),
    )
    tracer.event("one")
    tracer.event("two")
    tracer.event("three")
    assert [event.name for event in memory.events] == ["one", "two"]


@pytest.mark.asyncio
async def test_livekit_runner_passes_one_session_tracer_to_stt_tts_and_observers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    fake_session = FakeSession()

    class FakeTTS:
        async def aclose(self) -> None:
            captured["tts_closed"] = True

    async def create_tts(_settings: Settings, *, tracer: Tracer) -> FakeTTS:
        captured["tts_tracer"] = tracer
        return FakeTTS()

    def create_stt(_config: object, *, tracer: Tracer) -> object:
        captured["stt_tracer"] = tracer
        return object()

    monkeypatch.setattr(agent_module, "configure_trace_logging", lambda: None)
    monkeypatch.setattr(agent_module, "create_tts", create_tts)
    monkeypatch.setattr(agent_module, "FunASRSTT", create_stt)
    monkeypatch.setattr(agent_module, "create_deepseek_llm", lambda _settings: object())
    monkeypatch.setattr(agent_module.silero.VAD, "load", lambda **_options: object())
    monkeypatch.setattr(agent_module, "AgentSession", lambda **_options: fake_session)
    monkeypatch.setattr(agent_module, "_DefaultAgent", lambda _overlap_consumer: object())

    async def emit_segment(_frames: object) -> None:
        return None

    runner = LiveKitAgentRunner(
        Settings(runner="livekit", deepseek_api_key="test-key"),
        emit_segment,
        lambda _epoch: None,
    )
    assistant_texts: list[str] = []
    runner.set_text_sinks(lambda _text, _is_final: None, assistant_texts.append)
    await runner.start()
    assert captured["stt_tracer"] is captured["tts_tracer"] is runner._tracer  # noqa: SLF001
    assert set(fake_session.handlers) >= {
        "user_state_changed",
        "user_input_transcribed",
        "agent_state_changed",
        "conversation_item_added",
        "speech_created",
        "metrics_collected",
        "close",
    }
    assert fake_session.started
    text_output = fake_session.output.transcription
    assert isinstance(text_output, RoomlessTextOutput)
    await text_output.capture_text("首个")
    await text_output.capture_text("文本")
    assert assistant_texts == ["首个", "首个文本"]
    await runner.close()
    assert fake_session.closed and captured["tts_closed"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_type", [RuntimeError, asyncio.CancelledError])
async def test_livekit_runner_close_releases_output_and_tts_after_session_failure(
    failure_type: type[BaseException],
) -> None:
    events: list[str] = []

    class FailingSession:
        async def aclose(self) -> None:
            events.append("session")
            raise failure_type()

    class FakeOutput:
        async def close(self) -> None:
            events.append("output")

    class FakeTTS:
        async def aclose(self) -> None:
            events.append("tts")

    async def emit_segment(_frames: object) -> None:
        return None

    runner = LiveKitAgentRunner(
        Settings(runner="livekit", deepseek_api_key="test-key"),
        emit_segment,
        lambda _epoch: None,
    )
    runner._session = FailingSession()  # type: ignore[assignment]  # noqa: SLF001
    runner._output = FakeOutput()  # type: ignore[assignment]  # noqa: SLF001
    runner._tts = FakeTTS()  # noqa: SLF001

    with pytest.raises(failure_type):
        await runner.close()

    assert events == ["session", "output", "tts"]
    assert runner._session is None and runner._tts is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_livekit_runner_hard_deadline_continues_after_non_cooperative_session() -> None:
    release = asyncio.Event()
    events: list[str] = []
    baseline = detached_shutdown_task_count()

    class NonCooperativeSession:
        async def aclose(self) -> None:
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue

    class FakeOutput:
        async def close(self) -> None:
            events.append("output")

    class FakeTTS:
        async def aclose(self) -> None:
            events.append("tts")

    async def emit_segment(_frames: object) -> None:
        return None

    runner = LiveKitAgentRunner(
        Settings(
            runner="livekit",
            deepseek_api_key="test-key",
            agent_close_stage_timeout_seconds=0.02,
        ),
        emit_segment,
        lambda _epoch: None,
    )
    runner._session = NonCooperativeSession()  # type: ignore[assignment]  # noqa: SLF001
    runner._output = FakeOutput()  # type: ignore[assignment]  # noqa: SLF001
    runner._tts = FakeTTS()  # noqa: SLF001

    with pytest.raises(TimeoutError, match="AgentSession close timed out"):
        await runner.close()

    assert events == ["output", "tts"]
    assert detached_shutdown_task_count() == baseline + 1
    release.set()
    for _ in range(20):
        if detached_shutdown_task_count() == baseline:
            break
        await asyncio.sleep(0)
    assert detached_shutdown_task_count() == baseline
