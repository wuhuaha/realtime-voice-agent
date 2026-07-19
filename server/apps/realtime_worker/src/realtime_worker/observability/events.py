"""Bounded, structured timing events used by the experiment trace collector."""

from __future__ import annotations

import json
import logging
import sys
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class UtcClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True)
class TraceContext:
    trace_id: str
    room: str | None = None
    device_id: str | None = None
    turn_id: str | None = None
    segment_id: str | None = None
    request_id: str | None = None


@dataclass(frozen=True)
class TraceEvent:
    name: str
    at: datetime
    monotonic_ns: int
    context: TraceContext
    fields: Mapping[str, str | int | float | bool | None] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["at"] = self.at.isoformat()
        return result


class TraceSink(Protocol):
    def emit(self, event: TraceEvent) -> None: ...


class InMemoryTraceSink:
    """A bounded sink suitable for tests and short local debugging sessions."""

    def __init__(self, max_events: int = 512) -> None:
        self._events: deque[TraceEvent] = deque(maxlen=max_events)

    @property
    def events(self) -> tuple[TraceEvent, ...]:
        return tuple(self._events)

    def emit(self, event: TraceEvent) -> None:
        self._events.append(event)


class JsonLogTraceSink:
    """Writes correlation fields and timings, never transcript text or credentials."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("realtime_worker.trace")

    def emit(self, event: TraceEvent) -> None:
        self._logger.info("voice_trace", extra={"voice_trace": event.to_dict()})


class BoundedJsonLogTraceSink:
    """Bound total per-session log events without retaining event payloads in memory."""

    def __init__(self, *, max_events: int = 2048, sink: TraceSink | None = None) -> None:
        if max_events <= 0:
            raise ValueError("max_events must be positive")
        self._max_events = max_events
        self._sink = sink or JsonLogTraceSink()
        self._emitted = 0

    def emit(self, event: TraceEvent) -> None:
        if self._emitted >= self._max_events:
            return
        self._emitted += 1
        self._sink.emit(event)


class JsonTraceFormatter(logging.Formatter):
    """Serialize only the bounded voice trace payload as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload = getattr(record, "voice_trace", None)
        if not isinstance(payload, dict):
            payload = {
                "name": "trace_logging_error",
                "at": datetime.now(UTC).isoformat(),
                "monotonic_ns": time.monotonic_ns(),
                "fields": {"message": record.getMessage()},
            }
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def configure_trace_logging(stream: Any | None = None) -> None:
    """Install a dedicated JSONL handler without changing third-party logging."""

    trace_logger = logging.getLogger("realtime_worker.trace")
    if any(getattr(handler, "_voice_trace_handler", False) for handler in trace_logger.handlers):
        return
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonTraceFormatter())
    handler._voice_trace_handler = True  # type: ignore[attr-defined]
    trace_logger.addHandler(handler)
    trace_logger.setLevel(logging.INFO)
    trace_logger.propagate = False


_TURN_MILESTONES = (
    "user_speech_started",
    "asr_first_interim",
    "user_speech_ended",
    "asr_final",
    "eot_committed",
    "llm_requested",
    "tts_requested",
    "tts_first_pcm",
    "agent_audio_published",
)
_OPTIONAL_MILESTONES = (
    "asr_request_started",
    "asr_stream_ready",
    "asr_provider_interim",
    "asr_provider_final",
)
_REPEATABLE_MILESTONES = {
    "user_speech_started",
    "asr_first_interim",
    "user_speech_ended",
    "asr_final",
    "asr_request_started",
    "asr_stream_ready",
    "asr_provider_interim",
    "asr_provider_final",
}


class Tracer:
    def __init__(
        self,
        context: TraceContext,
        sink: TraceSink,
        clock: Clock | None = None,
        monotonic_ns_fn: Callable[[], int] | None = None,
    ) -> None:
        self._context = context
        self._sink = sink
        self._clock = clock or UtcClock()
        self._monotonic_ns = monotonic_ns_fn or time.monotonic_ns
        self._current_turn_id: str | None = None
        self._turn_sequence = 0
        self._turn_milestones: dict[str, dict[str, int]] = {}
        self._turn_provider_metrics: dict[str, dict[str, float]] = {}

    @property
    def current_turn_id(self) -> str | None:
        return self._current_turn_id

    def bind_turn(self, turn_id: str) -> None:
        self._current_turn_id = turn_id

    def ensure_turn(self) -> str:
        if self._current_turn_id is None:
            return self.begin_turn()
        return self._current_turn_id

    def begin_turn(self) -> str:
        """Start a new semantic turn without discarding an older playback turn's metrics."""

        self._turn_sequence += 1
        self._current_turn_id = f"turn-{self._turn_sequence:06d}"
        return self._current_turn_id

    def clear_turn(self, turn_id: str) -> None:
        if self._current_turn_id == turn_id:
            self._current_turn_id = None
        self._turn_milestones.pop(turn_id, None)
        self._turn_provider_metrics.pop(turn_id, None)

    def event(self, name: str, **fields: str | int | float | bool | None) -> None:
        event_fields = dict(fields)
        if "turn_id" not in event_fields and self._current_turn_id is not None:
            event_fields["turn_id"] = self._current_turn_id
        monotonic_ns = self._monotonic_ns()
        turn_id = event_fields.get("turn_id")
        if isinstance(turn_id, str):
            if name in _TURN_MILESTONES or name in _OPTIONAL_MILESTONES:
                milestones = self._turn_milestones.setdefault(turn_id, {})
                if name in _REPEATABLE_MILESTONES:
                    milestones[name] = monotonic_ns
                else:
                    milestones.setdefault(name, monotonic_ns)
            if name == "provider_metrics":
                self._capture_provider_metrics(turn_id, event_fields)
        self._sink.emit(
            TraceEvent(
                name=name,
                at=self._clock.now(),
                monotonic_ns=monotonic_ns,
                context=self._context,
                fields=event_fields,
            )
        )

    def emit_turn_summary(self, turn_id: str, *, phase: str = "first_audio") -> None:
        milestones = self._turn_milestones.get(turn_id, {})
        missing = [name for name in _TURN_MILESTONES if name not in milestones]
        fields: dict[str, str | int | float | bool | None] = {
            "turn_id": turn_id,
            "phase": phase,
            "status": "incomplete" if missing else "complete",
            "missing_stages": ",".join(missing),
        }
        pairs = {
            "speech_to_first_interim_ms": ("user_speech_started", "asr_first_interim"),
            "speech_duration_ms": ("user_speech_started", "user_speech_ended"),
            "speech_end_to_asr_final_ms": ("user_speech_ended", "asr_final"),
            "asr_final_to_eot_ms": ("asr_final", "eot_committed"),
            "eot_to_llm_ms": ("eot_committed", "llm_requested"),
            "llm_to_tts_request_ms": ("llm_requested", "tts_requested"),
            "tts_ttfb_ms": ("tts_requested", "tts_first_pcm"),
            "tts_first_pcm_to_agent_audio_ms": ("tts_first_pcm", "agent_audio_published"),
            "speech_end_to_agent_audio_ms": ("user_speech_ended", "agent_audio_published"),
            "turn_total_ms": ("user_speech_started", "agent_audio_published"),
            "asr_connect_ms": ("asr_request_started", "asr_stream_ready"),
            "asr_ready_to_first_interim_ms": ("asr_stream_ready", "asr_provider_interim"),
            "asr_ready_to_final_ms": ("asr_stream_ready", "asr_provider_final"),
            "asr_provider_to_agent_final_ms": ("asr_provider_final", "asr_final"),
        }
        invalid_pairs: list[str] = []
        for field_name, (start_name, end_name) in pairs.items():
            start = milestones.get(start_name)
            end = milestones.get(end_name)
            if start is not None and end is not None:
                if end < start:
                    invalid_pairs.append(f"{start_name}->{end_name}")
                else:
                    fields[field_name] = round((end - start) / 1_000_000, 3)
        if invalid_pairs:
            fields["status"] = "incomplete"
            fields["invalid_stages"] = ",".join(invalid_pairs)
        fields.update(self._turn_provider_metrics.get(turn_id, {}))
        self.event("turn_latency_summary", **fields)

    def _capture_provider_metrics(
        self,
        turn_id: str,
        fields: Mapping[str, str | int | float | bool | None],
    ) -> None:
        metric_type = fields.get("metric_type")
        prefixes = {
            "EOUMetrics": "eou",
            "LLMMetrics": "llm",
            "STTMetrics": "stt",
            "TTSMetrics": "tts_provider",
        }
        prefix = prefixes.get(metric_type) if isinstance(metric_type, str) else None
        if prefix is None:
            return
        captured = self._turn_provider_metrics.setdefault(turn_id, {})
        for name in (
            "duration",
            "ttfb",
            "ttft",
            "audio_duration",
            "end_of_utterance_delay",
            "transcription_delay",
            "on_user_turn_completed_delay",
        ):
            value = fields.get(name)
            if isinstance(value, int | float) and not isinstance(value, bool):
                captured.setdefault(f"{prefix}_{name}_ms", round(float(value) * 1000, 3))


def redact_exception(error: BaseException) -> str:
    """Keep provider error classification without serializing arbitrary response bodies."""

    return type(error).__name__
