"""Deterministic and roomless LiveKit Agent runners behind one small contract."""

from __future__ import annotations

import asyncio
import logging
import math
import struct
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol

from livekit import rtc
from livekit.agents import Agent, AgentSession
from livekit.agents.voice import io
from livekit.plugins import silero

from .audio import PCM_SAMPLE_RATE, PCM_SAMPLES, PcmFrame
from .config import Settings
from .observability.events import (
    BoundedJsonLogTraceSink,
    TraceContext,
    Tracer,
    configure_trace_logging,
)
from .providers.deepseek_llm import create_deepseek_llm
from .providers.funasr_stt import FunASRStreamConfig, FunASRSTT
from .providers.tts_factory import create_tts

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AgentOutputSegment:
    producer_epoch: int
    frames: list[PcmFrame]


SegmentSink = Callable[[AgentOutputSegment], Awaitable[None]]
StopSink = Callable[[int], None]
UserTranscriptSink = Callable[[str, bool], None]
AssistantTextSink = Callable[[str], None]


class AgentRunner(Protocol):
    async def start(self) -> None: ...

    async def push_audio(self, frame: PcmFrame) -> None: ...

    async def commit_text(self, text: str) -> None: ...

    async def playback_started(self, created_at: float) -> None: ...

    async def playback_finished(self, position: float, interrupted: bool) -> None: ...

    async def interrupt(self) -> int: ...

    async def close(self) -> None: ...


class DeterministicAgentRunner:
    def __init__(self, emit_segment: SegmentSink) -> None:
        self._emit_segment = emit_segment
        self._closed = False
        self._input_frames = 0
        self._responded = False
        self._producer_epoch = 1
        self._assistant_text_sink: AssistantTextSink | None = None

    async def start(self) -> None:
        return None

    async def push_audio(self, frame: PcmFrame) -> None:
        self._input_frames += 1
        if self._input_frames >= 3 and not self._responded:
            self._responded = True
            await self.commit_text("deterministic turn")

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

    def set_text_sinks(
        self,
        user_transcript: UserTranscriptSink,
        assistant_text: AssistantTextSink,
    ) -> None:
        self._assistant_text_sink = assistant_text

    async def playback_started(self, created_at: float) -> None:
        return None

    async def playback_finished(self, position: float, interrupted: bool) -> None:
        return None

    async def interrupt(self) -> int:
        self._producer_epoch += 1
        return self._producer_epoch

    async def close(self) -> None:
        self._closed = True


class RoomlessAudioInput(io.AudioInput):
    def __init__(self, capacity: int) -> None:
        super().__init__(label="roomless-input")
        self._queue: asyncio.Queue[rtc.AudioFrame | None] = asyncio.Queue(capacity)
        self._closed = False

    async def __anext__(self) -> rtc.AudioFrame:
        item = await self._queue.get()
        if item is None:
            raise StopAsyncIteration
        return item

    def push(self, frame: PcmFrame) -> None:
        if self._closed:
            return
        audio = rtc.AudioFrame(
            data=frame.pcm,
            sample_rate=PCM_SAMPLE_RATE,
            num_channels=1,
            samples_per_channel=PCM_SAMPLES,
        )
        try:
            self._queue.put_nowait(audio)
        except asyncio.QueueFull as exc:
            raise BufferError("LiveKit input queue is full") from exc

    def close(self) -> None:
        self._closed = True
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
    def __init__(self, emit_segment: SegmentSink, stop_playback: StopSink, *, max_segment_frames: int = 1_500) -> None:
        if max_segment_frames <= 0:
            raise ValueError("max_segment_frames must be positive")
        super().__init__(
            label="roomless-output",
            capabilities=io.AudioOutputCapabilities(pause=False),
            sample_rate=PCM_SAMPLE_RATE,
        )
        self._emit_segment = emit_segment
        self._stop_playback = stop_playback
        self._max_segment_frames = max_segment_frames
        self._frames: list[PcmFrame] = []
        self._pending_pcm = bytearray()
        self._callback_sizes: dict[int, int] = {}
        self._callback_size_overflow = 0
        self._frames_epoch: int | None = None
        self._producer_epoch = 1
        self._sequence = 0
        self._task: asyncio.Task[None] | None = None
        self._segment_rejected = False
        self._closed = False

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        if self._closed:
            return
        if self._segment_rejected:
            raise BufferError("LiveKit output segment was already rejected; waiting for flush")
        await super().capture_frame(frame)
        pcm = bytes(frame.data)
        if len(pcm) in self._callback_sizes or len(self._callback_sizes) < 8:
            self._callback_sizes[len(pcm)] = self._callback_sizes.get(len(pcm), 0) + 1
        else:
            self._callback_size_overflow += 1
        frame_bytes = PCM_SAMPLES * 2
        complete_chunks = (len(self._pending_pcm) + len(pcm)) // frame_bytes
        received_frames = len(self._frames) + complete_chunks
        if received_frames > self._max_segment_frames:
            self._frames.clear()
            self._pending_pcm.clear()
            self._frames_epoch = None
            self._segment_rejected = True
            frame_seconds = PCM_SAMPLES / PCM_SAMPLE_RATE
            raise BufferError(
                "LiveKit output segment exceeded its bounded frame limit: "
                f"received_frames={received_frames} limit_frames={self._max_segment_frames} "
                f"received_seconds={received_frames * frame_seconds:.3f} "
                f"limit_seconds={self._max_segment_frames * frame_seconds:.3f}"
            )
        self._pending_pcm.extend(pcm)
        for _ in range(complete_chunks):
            chunk = bytes(self._pending_pcm[:frame_bytes])
            del self._pending_pcm[:frame_bytes]
            if self._frames_epoch is None:
                self._frames_epoch = self._producer_epoch
            self._frames.append(PcmFrame(0, self._sequence, self._sequence * PCM_SAMPLES, chunk))
            self._sequence += 1

    def flush(self) -> None:
        super().flush()
        if self._closed or self._segment_rejected:
            self._frames.clear()
            self._pending_pcm.clear()
            self._reset_callback_diagnostics()
            self._frames_epoch = None
            self._segment_rejected = False
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
            received_frames = len(self._frames) + 1
            if received_frames > self._max_segment_frames:
                self._frames.clear()
                self._pending_pcm.clear()
                self._frames_epoch = None
                self._segment_rejected = False
                frame_seconds = PCM_SAMPLES / PCM_SAMPLE_RATE
                raise BufferError(
                    "LiveKit output segment exceeded its bounded frame limit: "
                    f"received_frames={received_frames} limit_frames={self._max_segment_frames} "
                    f"received_seconds={received_frames * frame_seconds:.3f} "
                    f"limit_seconds={self._max_segment_frames * frame_seconds:.3f}"
                )
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
        if self._task is not None and not self._task.done():
            raise BufferError("previous Direct audio segment is still pending")
        assert producer_epoch is not None
        self._task = asyncio.create_task(
            self._emit_segment(AgentOutputSegment(producer_epoch, frames)),
            name="roomless-output-segment",
        )

    def clear_buffer(self) -> None:
        producer_epoch = self.advance_producer_epoch()
        super().clear_buffer()
        super().flush()
        self._stop_playback(producer_epoch)

    def advance_producer_epoch(self) -> int:
        self._producer_epoch += 1
        self._frames.clear()
        self._pending_pcm.clear()
        self._reset_callback_diagnostics()
        self._frames_epoch = None
        self._segment_rejected = False
        return self._producer_epoch

    async def close(self) -> None:
        self._closed = True
        self._frames.clear()
        self._pending_pcm.clear()
        self._reset_callback_diagnostics()
        self._frames_epoch = None
        self._segment_rejected = False
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)

    def _reset_callback_diagnostics(self) -> None:
        self._callback_sizes.clear()
        self._callback_size_overflow = 0


class _DefaultAgent(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions=(
                "你是一个简洁、自然的中文语音助手。只输出适合直接朗读的纯文本，不使用 Markdown。"
                "不知道时明确说明，不虚构设备状态。"
            )
        )


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
def _load_vad(activation_threshold: float):  # type: ignore[no-untyped-def]
    return silero.VAD.load(
        force_cpu=True,
        sample_rate=PCM_SAMPLE_RATE,
        activation_threshold=activation_threshold,
    )


class LiveKitAgentRunner:
    def __init__(self, settings: Settings, emit_segment: SegmentSink, stop_playback: StopSink) -> None:
        settings.require_livekit()
        self._settings = settings
        self._input = RoomlessAudioInput(settings.media_queue_frames)
        self._output = RoomlessAudioOutput(
            emit_segment,
            stop_playback,
            max_segment_frames=settings.output_segment_max_frames,
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
        session = AgentSession(
            stt=FunASRSTT(FunASRStreamConfig.from_settings(self._settings), tracer=self._tracer),
            vad=_load_vad(self._settings.vad_activation_threshold),
            llm=create_deepseek_llm(self._settings),
            tts=self._tts,
            turn_handling={
                "turn_detection": "vad",
                "interruption": {
                    "enabled": True,
                    "min_duration": 0.5,
                    "min_words": 0,
                    "resume_false_interruption": False,
                },
            },
        )
        self._trace_observer = _register_livekit_observers(
            session,
            self._tracer,
            user_transcript=self._user_transcript_sink,
        )
        session.input.audio = self._input
        session.output.audio = self._output
        if self._assistant_text_sink is not None:
            self._text_output = RoomlessTextOutput(self._assistant_text_sink)
            session.output.transcription = self._text_output
        self._session = session
        await session.start(_DefaultAgent())

    async def push_audio(self, frame: PcmFrame) -> None:
        self._input.push(frame)

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
        self._input.close()
        if self._session is not None:
            await self._session.aclose()
        await self._output.close()
        closer = getattr(self._tts, "aclose", None)
        if closer is not None:
            await closer()


def create_runner(settings: Settings, emit_segment: SegmentSink, stop_playback: StopSink) -> AgentRunner:
    if settings.runner == "livekit":
        return LiveKitAgentRunner(settings, emit_segment, stop_playback)
    return DeterministicAgentRunner(emit_segment)


def _register_livekit_observers(
    session: Any,
    tracer: Tracer,
    *,
    user_transcript: UserTranscriptSink | None = None,
) -> _LiveKitPlaybackTrace:
    """Project public AgentSession events into PII-free phase and latency events."""

    interim_seen = False
    speech_turn_ids: dict[str, str] = {}
    agent_state = "initializing"
    response_turn_id: str | None = None
    playback_trace = _LiveKitPlaybackTrace(tracer)

    def ensure_turn() -> str:
        return tracer.ensure_turn()

    def on_user_state(event: object) -> None:
        nonlocal interim_seen
        new_state = getattr(event, "new_state", None)
        old_state = getattr(event, "old_state", None)
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
        nonlocal agent_state, response_turn_id
        state = getattr(event, "new_state", None)
        agent_state = state if isinstance(state, str) else agent_state
        if state == "thinking":
            response_turn_id = ensure_turn()
            tracer.event("llm_requested", turn_id=response_turn_id)
        elif state == "speaking":
            playback_trace.freeze_turn(response_turn_id or ensure_turn())

    def on_conversation_item(event: object) -> None:
        item = getattr(event, "item", None)
        role = getattr(item, "role", None)
        if role == "user":
            tracer.event("eot_committed", turn_id=ensure_turn())
        elif role == "assistant":
            turn_id = playback_trace.turn_id or response_turn_id
            if turn_id is None:
                tracer.event("assistant_item_committed")
            else:
                tracer.event("assistant_item_committed", turn_id=turn_id)

    def on_speech_created(event: object) -> None:
        speech_id = str(getattr(getattr(event, "speech_handle", None), "id", "")) or None
        turn_id = ensure_turn()
        if speech_id is not None:
            if len(speech_turn_ids) >= 256:
                speech_turn_ids.pop(next(iter(speech_turn_ids)))
            speech_turn_ids[speech_id] = turn_id

    def on_metrics(event: object) -> None:
        metrics = getattr(event, "metrics", None)
        metric_type = type(metrics).__name__
        if metrics is None or metric_type == "VADMetrics":
            return
        speech_id_value = getattr(metrics, "speech_id", None)
        speech_id = speech_id_value if isinstance(speech_id_value, str) else None
        fields: dict[str, str | int | float | bool | None] = {
            "metric_type": metric_type,
            "turn_id": speech_turn_ids.get(speech_id) if speech_id is not None else tracer.current_turn_id,
            # Kept as a 1.6.5 compatibility source. Upgrade candidates must verify
            # a public replacement before removing this deprecated event.
            "source": "metrics_collected_compat",
        }
        for name in (
            "label",
            "request_id",
            "speech_id",
            "segment_id",
            "duration",
            "ttfb",
            "ttft",
            "audio_duration",
            "end_of_utterance_delay",
            "transcription_delay",
            "on_user_turn_completed_delay",
            "cancelled",
            "streamed",
        ):
            value = getattr(metrics, name, None)
            if isinstance(value, str | int | float | bool) or value is None:
                fields[name] = value
        tracer.event("provider_metrics", **fields)

    def on_close(event: object) -> None:
        reason_value = getattr(event, "reason", "unknown")
        reason = getattr(reason_value, "value", str(reason_value))
        tracer.event("session_closed", reason=reason, has_error=getattr(event, "error", None) is not None)

    on = session.on
    on("user_state_changed", on_user_state)
    on("user_input_transcribed", on_user_transcript)
    on("agent_state_changed", on_agent_state)
    on("conversation_item_added", on_conversation_item)
    on("speech_created", on_speech_created)
    on("metrics_collected", on_metrics)
    on("close", on_close)
    return playback_trace
