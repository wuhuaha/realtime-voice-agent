"""Deterministic and roomless LiveKit Agent runners behind one small contract."""

from __future__ import annotations

import asyncio
import logging
import math
import struct
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from typing import Any, Protocol

from livekit import rtc
from livekit.agents import Agent, AgentSession, StopResponse
from livekit.agents.voice import io
from livekit.plugins import silero

from .audio import PCM_SAMPLE_RATE, PCM_SAMPLES, PcmFrame
from .config import Settings
from .lifecycle import retain_shutdown_task
from .observability.events import (
    BoundedJsonLogTraceSink,
    TraceContext,
    Tracer,
    configure_trace_logging,
)
from .providers.deepseek_llm import create_deepseek_llm
from .providers.funasr_stt import FunASRStreamConfig, FunASRSTT
from .providers.tts_factory import create_tts
from .vad import ResettingVAD

logger = logging.getLogger(__name__)


def _consume_task_result(task: asyncio.Future[None]) -> None:
    if not task.cancelled():
        task.exception()


@dataclass(frozen=True, slots=True)
class AgentOutputSegment:
    producer_epoch: int
    frames: list[PcmFrame]


class AgentRunnerTerminalKind(StrEnum):
    OWNER_CLOSED = "owner_closed"
    RUNTIME_FAILED = "runtime_failed"


@dataclass(frozen=True, slots=True)
class AgentRunnerTerminal:
    kind: AgentRunnerTerminalKind


class AgentRunnerTerminatedError(RuntimeError):
    def __init__(self, terminal: AgentRunnerTerminal) -> None:
        super().__init__("AgentSession terminated unexpectedly")
        self.terminal = terminal


class _TerminalState:
    def __init__(self) -> None:
        self._future: asyncio.Future[AgentRunnerTerminal] | None = None
        self._result: AgentRunnerTerminal | None = None

    @property
    def future(self) -> asyncio.Future[AgentRunnerTerminal]:
        if self._future is None:
            self._future = asyncio.get_running_loop().create_future()
            if self._result is not None:
                self._future.set_result(self._result)
        return self._future

    def publish(self, result: AgentRunnerTerminal) -> bool:
        if self._result is not None:
            return False
        self._result = result
        if self._future is not None and not self._future.done():
            self._future.set_result(result)
        return True


SegmentSink = Callable[[AgentOutputSegment], Awaitable[None]]
StopSink = Callable[[int], None]
UserTranscriptSink = Callable[[str, bool], bool | None]
AssistantTextSink = Callable[[str], None]


class AgentRunner(Protocol):
    @property
    def terminal(self) -> asyncio.Future[AgentRunnerTerminal]: ...

    async def start(self) -> None: ...

    async def push_audio(self, frame: PcmFrame) -> None: ...

    async def commit_text(self, text: str) -> None: ...

    def set_response_gate(self, active: bool) -> None: ...

    async def playback_started(self, created_at: float) -> None: ...

    async def playback_finished(self, position: float, interrupted: bool) -> None: ...

    async def interrupt(self) -> int: ...

    async def close(self) -> None: ...


class RoomlessAudioInputOverloadedError(BufferError):
    def __init__(self, *, qsize: int, capacity: int) -> None:
        super().__init__("LiveKit input queue is full")
        self.source = "pcm_input"
        self.qsize = qsize
        self.capacity = capacity


class RoomlessOutputDeliveryError(RuntimeError):
    """A buffered output segment could not reach the binding owner."""


class DeterministicAgentRunner:
    def __init__(self, emit_segment: SegmentSink, response_end: StopSink | None = None) -> None:
        self._emit_segment = emit_segment
        self._response_end = response_end
        self._closed = False
        self._input_frames = 0
        self._responded = False
        self._producer_epoch = 1
        self._user_transcript_sink: UserTranscriptSink | None = None
        self._assistant_text_sink: AssistantTextSink | None = None
        self._terminal_state = _TerminalState()
        self._response_task: asyncio.Task[None] | None = None

    @property
    def terminal(self) -> asyncio.Future[AgentRunnerTerminal]:
        return self._terminal_state.future

    async def start(self) -> None:
        return None

    async def push_audio(self, frame: PcmFrame) -> None:
        self._input_frames += 1
        if self._input_frames >= 3 and not self._responded:
            self._responded = True
            if self._user_transcript_sink is not None:
                self._user_transcript_sink("deterministic", False)
                self._user_transcript_sink("deterministic turn", True)
            self._response_task = asyncio.create_task(
                self.commit_text("deterministic turn"),
                name="deterministic-agent-response",
            )
            self._response_task.add_done_callback(self._response_done)

    def _response_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        if task.exception() is not None:
            self._terminal_state.publish(AgentRunnerTerminal(AgentRunnerTerminalKind.RUNTIME_FAILED))

    async def commit_text(self, text: str) -> None:
        if self._closed or not text.strip():
            return
        if self._assistant_text_sink is not None:
            self._assistant_text_sink(text)
        frames: list[PcmFrame] = []
        frequency = 440 + min(len(text), 20) * 10
        for sequence in range(10):
            samples = (
                int(1800 * math.sin(2 * math.pi * frequency * (sequence * PCM_SAMPLES + index) / PCM_SAMPLE_RATE))
                for index in range(PCM_SAMPLES)
            )
            pcm = struct.pack(f"<{PCM_SAMPLES}h", *samples)
            frames.append(PcmFrame(0, sequence, sequence * PCM_SAMPLES, pcm))
        await self._emit_segment(AgentOutputSegment(self._producer_epoch, frames))
        if self._response_end is not None:
            self._response_end(self._producer_epoch)

    def set_text_sinks(
        self,
        user_transcript: UserTranscriptSink,
        assistant_text: AssistantTextSink,
    ) -> None:
        self._user_transcript_sink = user_transcript
        self._assistant_text_sink = assistant_text

    def set_response_gate(self, active: bool) -> None:
        del active
        return None

    async def playback_started(self, created_at: float) -> None:
        return None

    async def playback_finished(self, position: float, interrupted: bool) -> None:
        return None

    async def interrupt(self) -> int:
        self._producer_epoch += 1
        return self._producer_epoch

    async def close(self) -> None:
        self._closed = True
        if self._response_task is not None and not self._response_task.done():
            self._response_task.cancel()
            await asyncio.gather(self._response_task, return_exceptions=True)
        self._terminal_state.publish(AgentRunnerTerminal(AgentRunnerTerminalKind.OWNER_CLOSED))


class RoomlessAudioInput(io.AudioInput):
    def __init__(self, capacity: int, *, queue_timeout_seconds: float = 0.2) -> None:
        if capacity <= 0 or queue_timeout_seconds <= 0:
            raise ValueError("Roomless input queue limits must be positive")
        super().__init__(label="roomless-input")
        self._queue: asyncio.Queue[rtc.AudioFrame | None] = asyncio.Queue(capacity)
        self._queue_timeout_seconds = queue_timeout_seconds
        self._closed = False
        self._closed_event = asyncio.Event()
        self._closed_terminal: AgentRunnerTerminal | None = None

    async def __anext__(self) -> rtc.AudioFrame:
        item = await self._queue.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def push(self, frame: PcmFrame) -> None:
        if self._closed:
            if self._closed_terminal is not None:
                raise AgentRunnerTerminatedError(self._closed_terminal)
            return
        audio = rtc.AudioFrame(
            data=frame.pcm,
            sample_rate=PCM_SAMPLE_RATE,
            num_channels=1,
            samples_per_channel=PCM_SAMPLES,
        )
        try:
            self._queue.put_nowait(audio)
        except asyncio.QueueFull:
            put_task = asyncio.create_task(self._queue.put(audio), name="roomless-input-put")
            close_task = asyncio.create_task(self._closed_event.wait(), name="roomless-input-close-wait")
            try:
                done, _ = await asyncio.wait(
                    {put_task, close_task},
                    timeout=self._queue_timeout_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if close_task in done or self._closed:
                    if self._closed_terminal is not None:
                        raise AgentRunnerTerminatedError(self._closed_terminal)
                    return
                if put_task not in done:
                    raise RoomlessAudioInputOverloadedError(
                        qsize=self._queue.qsize(),
                        capacity=self._queue.maxsize,
                    )
                await put_task
            finally:
                for task in (put_task, close_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(put_task, close_task, return_exceptions=True)

    def close(self, terminal: AgentRunnerTerminal | None = None) -> None:
        if terminal is not None:
            self._closed_terminal = terminal
        if self._closed:
            return
        self._closed = True
        self._closed_event.set()
        while not self._queue.empty():
            self._queue.get_nowait()
        self._queue.put_nowait(None)


class RoomlessTextOutput(io.TextOutput):
    def __init__(self, sink: AssistantTextSink) -> None:
        super().__init__(label="roomless-transcription", next_in_chain=None)
        self._sink = sink
        self._text = ""

    async def capture_text(self, text: str) -> None:
        if not text:
            return
        self._text += text
        self._sink(self._text)

    def flush(self) -> None:
        self._text = ""


class RoomlessAudioOutput(io.AudioOutput):
    def __init__(
        self,
        emit_segment: SegmentSink,
        response_end: StopSink,
        *,
        max_segment_frames: int = 1_500,
        report_failure: Callable[[BaseException], None] | None = None,
    ) -> None:
        if max_segment_frames <= 0:
            raise ValueError("max_segment_frames must be positive")
        super().__init__(
            label="roomless-output",
            capabilities=io.AudioOutputCapabilities(pause=False),
            sample_rate=PCM_SAMPLE_RATE,
        )
        self._emit_segment = emit_segment
        self._response_end = response_end
        self._report_failure = report_failure
        self._segment_failure: BaseException | None = None
        self._max_segment_frames = max_segment_frames
        self._frames: list[PcmFrame] = []
        self._pending_pcm = bytearray()
        self._callback_sizes: dict[int, int] = {}
        self._callback_size_overflow = 0
        self._frames_epoch: int | None = None
        self._producer_epoch = 1
        self._sequence = 0
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        if self._closed:
            return
        await super().capture_frame(frame)
        pcm = bytes(frame.data)
        if len(pcm) in self._callback_sizes or len(self._callback_sizes) < 8:
            self._callback_sizes[len(pcm)] = self._callback_sizes.get(len(pcm), 0) + 1
        else:
            self._callback_size_overflow += 1
        frame_bytes = PCM_SAMPLES * 2
        self._pending_pcm.extend(pcm)
        while len(self._pending_pcm) >= frame_bytes:
            chunk = bytes(self._pending_pcm[:frame_bytes])
            del self._pending_pcm[:frame_bytes]
            if self._frames_epoch is None:
                self._frames_epoch = self._producer_epoch
            self._frames.append(PcmFrame(0, self._sequence, self._sequence * PCM_SAMPLES, chunk))
            self._sequence += 1
            if len(self._frames) == self._max_segment_frames:
                await self._emit_buffered_segment()

    def flush(self) -> None:
        super().flush()
        if self._closed:
            self._frames.clear()
            self._pending_pcm.clear()
            self._reset_callback_diagnostics()
            self._frames_epoch = None
            return
        pending_bytes = len(self._pending_pcm)
        if self._callback_sizes or self._callback_size_overflow:
            logger.info(
                "LiveKit output PCM callbacks sizes=%s overflow=%d final_pending_bytes=%d",
                sorted(self._callback_sizes.items()),
                self._callback_size_overflow,
                pending_bytes,
            )
        self._reset_callback_diagnostics()
        if self._pending_pcm:
            if self._frames_epoch is None:
                self._frames_epoch = self._producer_epoch
            final_pcm = bytes(self._pending_pcm).ljust(PCM_SAMPLES * 2, b"\x00")
            self._pending_pcm.clear()
            self._frames.append(PcmFrame(0, self._sequence, self._sequence * PCM_SAMPLES, final_pcm))
            self._sequence += 1
        frames, self._frames = self._frames, []
        producer_epoch, self._frames_epoch = self._frames_epoch, None
        if not frames:
            return
        assert producer_epoch is not None
        task = asyncio.create_task(
            self._emit_segment(AgentOutputSegment(producer_epoch, frames)),
            name="roomless-output-segment",
        )
        self._tasks.add(task)
        task.add_done_callback(self._on_segment_task_done)

    async def _emit_buffered_segment(self) -> None:
        frames, self._frames = self._frames, []
        producer_epoch, self._frames_epoch = self._frames_epoch, None
        if not frames:
            return
        assert producer_epoch is not None
        await self._emit_segment(AgentOutputSegment(producer_epoch, frames))

    async def wait_for_playout(self) -> io.PlaybackFinishedEvent:
        producer_epoch = self._producer_epoch
        if self._tasks:
            try:
                await asyncio.gather(*tuple(self._tasks))
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if not self._closed or (current is not None and current.cancelling()):
                    raise
            except BaseException as exc:
                if self._segment_failure is None:
                    self._segment_failure = exc
        if self._segment_failure is not None:
            raise RoomlessOutputDeliveryError("output segment delivery failed") from self._segment_failure
        self._response_end(producer_epoch)
        result = await super().wait_for_playout()
        if self._producer_epoch == producer_epoch:
            self.advance_producer_epoch()
        return result

    def clear_buffer(self) -> None:
        self.advance_producer_epoch()
        super().clear_buffer()
        super().flush()

    def advance_producer_epoch(self) -> int:
        self._producer_epoch += 1
        self._frames.clear()
        self._pending_pcm.clear()
        self._reset_callback_diagnostics()
        self._frames_epoch = None
        return self._producer_epoch

    def _on_segment_task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:
            self._segment_failure = exception
            if self._report_failure is not None:
                self._report_failure(exception)
            logger.error(
                "Roomless output segment delivery failed",
                exc_info=(type(exception), exception, exception.__traceback__),
            )

    async def close(self) -> None:
        self._closed = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._close_once(),
                name="roomless-audio-output-close",
            )
            self._close_task.add_done_callback(_consume_task_result)
        try:
            await asyncio.shield(self._close_task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                retain_shutdown_task(
                    self._close_task,
                    task_name="roomless-audio-output-close",
                    cancel_requested=False,
                )
            raise

    async def _close_once(self) -> None:
        self._closed = True
        self._frames.clear()
        self._pending_pcm.clear()
        self._reset_callback_diagnostics()
        self._frames_epoch = None
        if self._tasks:
            tasks = tuple(self._tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._tasks.clear()
        while self._pending_playback_count > 0:
            self.on_playback_finished(playback_position=0.0, interrupted=True)

    def _reset_callback_diagnostics(self) -> None:
        self._callback_sizes.clear()
        self._callback_size_overflow = 0


class _PlaybackResponseGate:
    """Suppress LLM replies for user turns contaminated by endpoint playback."""

    def __init__(self, pending_limit: int = 8) -> None:
        self._response_active = False
        self._turn_sequence = 0
        self._active_turn: int | None = None
        self._active_turn_contaminated = False
        self._pending: deque[tuple[int, str]] = deque(maxlen=pending_limit)

    def set_response_active(self, active: bool) -> None:
        self._response_active = active

    def user_state_changed(self, old_state: object, new_state: object) -> None:
        del old_state
        if new_state != "speaking":
            return
        if not self._response_active:
            # A clean turn proves any older unconsumed marker belonged to a hook
            # LiveKit skipped while an uninterruptible response was active.
            self._pending.clear()
        self._turn_sequence += 1
        self._active_turn = self._turn_sequence
        self._active_turn_contaminated = self._response_active

    def observe_transcript(self, text: str, is_final: bool) -> None:
        if not text:
            return
        if self._active_turn is None:
            self._turn_sequence += 1
            self._active_turn = self._turn_sequence
        if self._response_active:
            self._active_turn_contaminated = True
        if not is_final:
            return
        if self._active_turn_contaminated:
            self._pending.append((self._active_turn, text))
        self._active_turn = None
        self._active_turn_contaminated = False

    def should_suppress(self, message: object) -> bool:
        text = getattr(message, "raw_text_content", None)
        candidate = text if isinstance(text, str) else ""
        if self._pending:
            _, expected = self._pending[0]
            if candidate == expected:
                self._pending.popleft()
                return True
            # Hooks are ordered. A mismatch means the marked hook was skipped;
            # never let a stale marker suppress an unrelated future turn.
            self._pending.clear()
        return self._response_active

    def reset(self) -> None:
        self._response_active = False
        self._active_turn = None
        self._active_turn_contaminated = False
        self._pending.clear()


class _DefaultAgent(Agent):
    def __init__(self, should_suppress_turn: Callable[[object], bool]) -> None:
        self._should_suppress_turn = should_suppress_turn
        super().__init__(
            instructions=(
                "你是一个简洁、自然的中文语音助手。只输出适合直接朗读的纯文本，不使用 Markdown。"
                "不知道时明确说明，不虚构设备状态。"
            )
        )

    async def on_user_turn_completed(self, turn_ctx: Any, new_message: Any) -> None:
        del turn_ctx
        if self._should_suppress_turn(new_message):
            raise StopResponse


class _LiveKitPlaybackTrace:
    def __init__(self, tracer: Tracer) -> None:
        self._tracer = tracer
        self._playback_turn_id: str | None = None

    def freeze_turn(self, turn_id: str) -> None:
        if self._playback_turn_id is None:
            self._playback_turn_id = turn_id

    @property
    def turn_id(self) -> str | None:
        return self._playback_turn_id

    def playback_started(self, created_at: float) -> None:
        turn_id = self._playback_turn_id or self._tracer.ensure_turn()
        self._playback_turn_id = turn_id
        self._tracer.event(
            "endpoint_playback_started",
            turn_id=turn_id,
            started_at_unix_ms=round(created_at * 1000),
        )
        self._tracer.event("agent_audio_published", turn_id=turn_id)
        self._tracer.emit_turn_summary(turn_id)

    def playback_finished(self, position: float, interrupted: bool) -> None:
        turn_id = self._playback_turn_id or self._tracer.ensure_turn()
        self._tracer.event(
            "endpoint_playback_finished",
            turn_id=turn_id,
            playback_position_ms=round(position * 1000),
            interrupted=interrupted,
        )
        self._tracer.emit_turn_summary(turn_id, phase="completed")
        self._tracer.clear_turn(turn_id)
        self._playback_turn_id = None


@lru_cache(maxsize=8)
def _load_vad(activation_threshold: float, deactivation_threshold: float):  # type: ignore[no-untyped-def]
    return silero.VAD.load(
        force_cpu=True,
        sample_rate=PCM_SAMPLE_RATE,
        activation_threshold=activation_threshold,
        deactivation_threshold=deactivation_threshold,
    )


class LiveKitAgentRunner:
    def __init__(self, settings: Settings, emit_segment: SegmentSink, response_end: StopSink) -> None:
        settings.require_livekit()
        self._settings = settings
        self._terminal_state = _TerminalState()
        self._input = RoomlessAudioInput(
            settings.media_queue_frames,
            queue_timeout_seconds=settings.rva_queue_timeout_seconds,
        )
        self._output = RoomlessAudioOutput(
            emit_segment,
            response_end,
            max_segment_frames=settings.output_segment_max_frames,
            report_failure=self._handle_output_failure,
        )
        self._session: AgentSession | None = None
        self._tts: object | None = None
        self._tracer = Tracer(
            TraceContext(trace_id=f"trace-{uuid.uuid4().hex}"),
            BoundedJsonLogTraceSink(),
        )
        self._trace_observer: _LiveKitPlaybackTrace | None = None
        self._user_transcript_sink: UserTranscriptSink | None = None
        self._assistant_text_sink: AssistantTextSink | None = None
        self._text_output: RoomlessTextOutput | None = None
        self._response_gate = _PlaybackResponseGate()
        self._owner_closing = False
        self._close_owner_task: asyncio.Task[None] | None = None
        self._close_result: asyncio.Future[None] | None = None

    @property
    def terminal(self) -> asyncio.Future[AgentRunnerTerminal]:
        return self._terminal_state.future

    def set_text_sinks(
        self,
        user_transcript: UserTranscriptSink,
        assistant_text: AssistantTextSink,
    ) -> None:
        self._user_transcript_sink = user_transcript
        self._assistant_text_sink = assistant_text

    async def start(self) -> None:
        configure_trace_logging()
        self._tts = await create_tts(self._settings, tracer=self._tracer)
        vad = ResettingVAD(
            _load_vad(
                self._settings.vad_activation_threshold,
                self._settings.vad_deactivation_threshold,
            ),
            idle_reset_seconds=self._settings.vad_idle_reset_seconds,
        )
        session = AgentSession(
            stt=FunASRSTT(FunASRStreamConfig.from_settings(self._settings), tracer=self._tracer),
            vad=vad,
            llm=create_deepseek_llm(self._settings),
            tts=self._tts,
            turn_handling={
                "turn_detection": "vad",
                "interruption": {
                    "enabled": False,
                    "discard_audio_if_uninterruptible": False,
                    "min_duration": self._settings.agent_interruption_min_duration_seconds,
                    "min_words": 0,
                    "resume_false_interruption": False,
                },
            },
            aec_warmup_duration=None,
        )
        self._trace_observer = _register_livekit_observers(
            session,
            self._tracer,
            user_transcript=self._handle_user_transcript,
            user_state=self._handle_user_state,
            session_close=self._handle_session_close,
        )
        session.input.audio = self._input
        session.output.audio = self._output
        if self._assistant_text_sink is not None:
            self._text_output = RoomlessTextOutput(self._assistant_text_sink)
            session.output.transcription = self._text_output
        self._session = session
        await session.start(_DefaultAgent(self._response_gate.should_suppress))

    def _handle_user_state(self, old_state: object, new_state: object) -> None:
        self._response_gate.user_state_changed(old_state, new_state)

    def _handle_session_close(self, _event: object) -> None:
        kind = AgentRunnerTerminalKind.OWNER_CLOSED if self._owner_closing else AgentRunnerTerminalKind.RUNTIME_FAILED
        terminal = AgentRunnerTerminal(kind)
        if kind is AgentRunnerTerminalKind.RUNTIME_FAILED:
            self._input.close(terminal)
        self._terminal_state.publish(terminal)

    def _handle_output_failure(self, _error: BaseException) -> None:
        terminal = AgentRunnerTerminal(AgentRunnerTerminalKind.RUNTIME_FAILED)
        self._input.close(terminal)
        self._terminal_state.publish(terminal)

    def _handle_user_transcript(self, text: str, is_final: bool) -> None:
        sink = self._user_transcript_sink
        if sink is not None:
            sink(text, is_final)
        self._response_gate.observe_transcript(text, is_final)

    def set_response_gate(self, active: bool) -> None:
        self._response_gate.set_response_active(active)

    async def push_audio(self, frame: PcmFrame) -> None:
        await self._input.push(frame)

    async def commit_text(self, text: str) -> None:
        if self._session is None:
            raise RuntimeError("AgentSession is not started")
        self._session.generate_reply(user_input=text)

    async def playback_started(self, created_at: float) -> None:
        self._output.on_playback_started(created_at=created_at)
        if self._trace_observer is not None:
            self._trace_observer.playback_started(created_at)

    async def playback_finished(self, position: float, interrupted: bool) -> None:
        self._output.on_playback_finished(playback_position=position, interrupted=interrupted)
        if self._trace_observer is not None:
            self._trace_observer.playback_finished(position, interrupted)

    async def interrupt(self) -> int:
        if self._session is None:
            raise RuntimeError("AgentSession is not started")
        self._output.advance_producer_epoch()
        await self._session.interrupt(force=True)
        return self._output.advance_producer_epoch()

    async def close(self) -> None:
        if self._close_owner_task is None:
            self._close_result = asyncio.get_running_loop().create_future()
            self._close_result.add_done_callback(_consume_task_result)
            self._close_owner_task = asyncio.create_task(
                self._close_once(),
                name="livekit-agent-runner-close",
            )
            self._close_owner_task.add_done_callback(self._close_owner_done)
        assert self._close_result is not None
        try:
            await asyncio.shield(self._close_result)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                retain_shutdown_task(
                    self._close_owner_task,
                    task_name="livekit-agent-runner-close",
                    cancel_requested=False,
                )
            raise

    def _close_owner_done(self, task: asyncio.Task[None]) -> None:
        exception: BaseException | None
        if task.cancelled():
            exception = asyncio.CancelledError()
        else:
            exception = task.exception()
        if self._close_result is None or self._close_result.done():
            return
        if exception is not None:
            self._close_result.set_exception(exception)
        else:
            self._close_result.set_result(None)

    def _publish_close_failure(self, failure: BaseException) -> None:
        assert self._close_result is not None
        if not self._close_result.done():
            self._close_result.set_exception(failure)

    async def _close_once(self) -> None:
        self._owner_closing = True
        self._terminal_state.publish(AgentRunnerTerminal(AgentRunnerTerminalKind.OWNER_CLOSED))
        self._response_gate.reset()
        failures: list[BaseException] = []
        try:
            self._input.close()
        except BaseException as exc:
            failures.append(exc)
            self._publish_close_failure(failures[0])

        await self._close_stage(
            self._output.close(),
            task_name="livekit-agent-output-close",
            timeout_error=TimeoutError("LiveKit output close timed out"),
            failures=failures,
        )

        session, self._session = self._session, None
        if session is not None:
            await self._close_stage(
                session.aclose(),
                task_name="livekit-agent-session-close",
                timeout_error=TimeoutError("LiveKit AgentSession close timed out"),
                failures=failures,
            )

        tts, self._tts = self._tts, None
        closer = getattr(tts, "aclose", None)
        if closer is not None:
            await self._close_stage(
                closer(),
                task_name="livekit-agent-tts-close",
                timeout_error=TimeoutError("LiveKit TTS close timed out"),
                failures=failures,
            )

        assert self._close_result is not None
        if not self._close_result.done():
            if failures:
                self._close_result.set_exception(failures[0])
            else:
                self._close_result.set_result(None)

        if failures:
            raise failures[0]

    async def _close_stage(
        self,
        operation: Coroutine[object, object, None],
        *,
        task_name: str,
        timeout_error: TimeoutError,
        failures: list[BaseException],
    ) -> None:
        task = asyncio.create_task(operation, name=task_name)
        done, _ = await asyncio.wait(
            {task},
            timeout=self._settings.agent_close_stage_timeout_seconds,
        )
        if not done:
            self._publish_close_failure(failures[0] if failures else timeout_error)
            owner = asyncio.current_task()
            assert owner is not None
            retain_shutdown_task(
                owner,
                task_name=task_name,
                cancel_requested=False,
            )

        try:
            await task
        except BaseException as exc:
            failures.append(exc)
            self._publish_close_failure(failures[0])


def create_runner(settings: Settings, emit_segment: SegmentSink, response_end: StopSink) -> AgentRunner:
    if settings.runner == "livekit":
        return LiveKitAgentRunner(settings, emit_segment, response_end)
    return DeterministicAgentRunner(emit_segment, response_end)


def _register_livekit_observers(
    session: Any,
    tracer: Tracer,
    *,
    user_transcript: UserTranscriptSink | None = None,
    user_state: Callable[[object, object], None] | None = None,
    session_close: Callable[[object], None] | None = None,
) -> _LiveKitPlaybackTrace:
    """Project public AgentSession events into PII-free phase and latency events."""

    interim_seen = False
    agent_state = "initializing"
    response_turn_id: str | None = None
    response_turn_pending = False
    playback_trace = _LiveKitPlaybackTrace(tracer)

    def ensure_turn() -> str:
        return tracer.ensure_turn()

    def on_user_state(event: object) -> None:
        nonlocal interim_seen
        new_state = getattr(event, "new_state", None)
        old_state = getattr(event, "old_state", None)
        if user_state is not None:
            user_state(old_state, new_state)
        created_at = getattr(event, "created_at", None)
        source_at_ms = round(created_at * 1000) if isinstance(created_at, int | float) else None
        if new_state == "speaking":
            interim_seen = False
            response_in_progress = response_turn_id is not None and response_turn_id == tracer.current_turn_id
            playback_in_progress = (
                playback_trace.turn_id is not None and playback_trace.turn_id == tracer.current_turn_id
            )
            if agent_state in {"thinking", "speaking"} and (response_in_progress or playback_in_progress):
                tracer.begin_turn()
            tracer.event("user_speech_started", turn_id=ensure_turn(), source_at_unix_ms=source_at_ms)
        elif old_state == "speaking":
            tracer.event("user_speech_ended", turn_id=ensure_turn(), source_at_unix_ms=source_at_ms)

    def on_user_transcript(event: object) -> None:
        nonlocal interim_seen
        transcript = getattr(event, "transcript", None)
        is_final = bool(getattr(event, "is_final", False))
        if user_transcript is not None and isinstance(transcript, str) and transcript:
            user_transcript(transcript, is_final)
        if is_final:
            tracer.event("asr_final", turn_id=ensure_turn())
        else:
            turn_id = ensure_turn()
            tracer.event("asr_interim", turn_id=turn_id)
            if not interim_seen:
                interim_seen = True
                tracer.event("asr_first_interim", turn_id=turn_id)

    def on_agent_state(event: object) -> None:
        nonlocal agent_state, response_turn_id, response_turn_pending
        state = getattr(event, "new_state", None)
        agent_state = state if isinstance(state, str) else agent_state
        if state == "thinking":
            # A playback-contaminated VAD turn can make LiveKit briefly enter
            # thinking again before the original response reaches speaking.
            # Keep the first response owner until it is handed to playback.
            if not response_turn_pending and playback_trace.turn_id is None:
                response_turn_id = ensure_turn()
                response_turn_pending = True
                tracer.event("llm_requested", turn_id=response_turn_id)
        elif state == "speaking":
            playback_trace.freeze_turn(response_turn_id or ensure_turn())
            response_turn_pending = False

    def on_conversation_item(event: object) -> None:
        item = getattr(event, "item", None)
        role = getattr(item, "role", None)
        turn_id: str | None = None
        if role == "user":
            turn_id = ensure_turn()
            tracer.event("eot_committed", turn_id=turn_id)
        elif role == "assistant":
            turn_id = playback_trace.turn_id or response_turn_id
            if turn_id is None:
                tracer.event("assistant_item_committed")
            else:
                tracer.event("assistant_item_committed", turn_id=turn_id)

        metrics = getattr(item, "metrics", None)
        if turn_id is not None and isinstance(metrics, Mapping):
            fields: dict[str, str | int | float | bool | None] = {
                "metric_type": "ChatMessageMetrics",
                "source": "chat_message",
                "role": role,
                "turn_id": turn_id,
            }
            for name in (
                "transcription_delay",
                "end_of_turn_delay",
                "on_user_turn_completed_delay",
                "llm_node_ttft",
                "tts_node_ttfb",
                "playback_latency",
                "e2e_latency",
            ):
                value = metrics.get(name)
                if (
                    isinstance(value, int | float)
                    and not isinstance(value, bool)
                    and math.isfinite(value)
                ):
                    fields[name] = value
            if len(fields) > 4:
                tracer.event("provider_metrics", **fields)

    def on_session_usage(event: object) -> None:
        usage = getattr(event, "usage", None)
        model_usage = getattr(usage, "model_usage", None)
        if isinstance(model_usage, list):
            tracer.event("session_usage_updated", model_usage_count=len(model_usage))

    def on_close(event: object) -> None:
        if session_close is not None:
            session_close(event)
        reason_value = getattr(event, "reason", "unknown")
        reason = getattr(reason_value, "value", str(reason_value))
        tracer.event("session_closed", reason=reason, has_error=getattr(event, "error", None) is not None)

    on = session.on
    on("user_state_changed", on_user_state)
    on("user_input_transcribed", on_user_transcript)
    on("agent_state_changed", on_agent_state)
    on("conversation_item_added", on_conversation_item)
    on("session_usage_updated", on_session_usage)
    on("close", on_close)
    return playback_trace
