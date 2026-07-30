"""FastAPI WebSocket owner for the RVA WSS vertical runtime."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from realtime_worker.agent import (
    AgentOutputSegment,
    AgentRunner,
    AgentRunnerTerminalKind,
    AgentRunnerTerminatedError,
    RoomlessAudioInputOverloadedError,
)
from realtime_worker.interruption import InterruptionContext, InterruptionCoordinator, LayeredInterruptionPolicy
from realtime_worker.transport.udp_gateway import (
    UdpGrantExpiredError,
    UdpMediaGateway,
    UdpMediaSession,
    UdpProbeTimeoutError,
)
from realtime_worker.voice.session import PlaybackRef

from .binding import AgentControlPort, AudioInputPort, ControlEffect, InboundAudioPacket, RvaWssBinding
from .codec import FRAME_DURATION_MS, SAMPLE_RATE, SAMPLES_PER_PACKET, RvaOpusCodec, RvaOpusDecodeError
from .protocol import UDP_PROFILE, WSS_PROFILE, RvaBindingError, RvaMessageTooLarge
from .response import ResponseRecord

logger = logging.getLogger(__name__)

RunnerFactory = Callable[[Callable[[AgentOutputSegment], Awaitable[None]], Callable[[int], None]], AgentRunner]
CodecFactory = Callable[[], RvaOpusCodec]
Clock = Callable[[], float]
_TIMELINE_REANCHOR_SECONDS = 30.0
_TIMELINE_MAX_SKEW_RATIO = 1_000 / 1_000_000


class TextAwareRunner(Protocol):
    def set_text_sinks(
        self,
        user_transcript: Callable[[str, bool], bool],
        assistant_text: Callable[[str], None],
    ) -> None: ...


class RvaRuntimeError(RuntimeError):
    pass


class RvaOverloadedError(RvaRuntimeError):
    def __init__(
        self,
        message: str,
        *,
        source: str,
        qsize: int,
        capacity: int,
        media_age_ms: int = -1,
        dropped_packets: int = 0,
        fresh_packet_available: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.source = source
        self.qsize = qsize
        self.capacity = capacity
        self.media_age_ms = media_age_ms
        self.dropped_packets = dropped_packets
        self.fresh_packet_available = fresh_packet_available


class RvaHandshakeTimeoutError(RvaRuntimeError):
    pass


class RvaControlTimeoutError(RvaRuntimeError):
    pass


class RvaMediaSendTimeoutError(RvaRuntimeError):
    pass


class RvaRuntimeStartTimeoutError(RvaRuntimeError):
    pass


class RvaAgentRuntimeFailedError(RvaRuntimeError):
    pass


class RvaIdleTimeoutError(RvaRuntimeError):
    pass


class RvaMediaDecodeError(RvaRuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RvaRuntimeLimits:
    input_queue_packets: int = 8
    output_queue_items: int = 12
    max_segment_frames: int = 1_500
    queue_timeout_seconds: float = 0.2
    uplink_max_age_seconds: float = 0.6
    wire_send_timeout_seconds: float = 1.0
    handshake_timeout_seconds: float = 5.0
    runner_timeout_seconds: float = 5.0
    close_timeout_seconds: float = 5.0
    agent_close_stage_timeout_seconds: float = 2.0
    idle_timeout_seconds: float = 45.0
    playback_prebuffer_packets: int = 4
    max_consecutive_invalid_opus_packets: int = 8

    def __post_init__(self) -> None:
        positive = (
            self.input_queue_packets,
            self.output_queue_items,
            self.max_segment_frames,
            self.queue_timeout_seconds,
            self.uplink_max_age_seconds,
            self.wire_send_timeout_seconds,
            self.handshake_timeout_seconds,
            self.runner_timeout_seconds,
            self.close_timeout_seconds,
            self.agent_close_stage_timeout_seconds,
            self.idle_timeout_seconds,
            self.max_consecutive_invalid_opus_packets,
        )
        if any(value <= 0 for value in positive) or self.playback_prebuffer_packets < 0:
            raise ValueError("RVA runtime limits must be positive")


@dataclass(slots=True)
class _Outbound:
    kind: Literal["control", "segment", "response_end"]
    payload: str | AgentOutputSegment | int
    ack: asyncio.Future[None] | None = None
    assistant_text: str | None = None


@dataclass(frozen=True, slots=True)
class _QueuedAudio:
    packet: InboundAudioPacket
    received_at: float
    expected_at: float
    deadline_at: float


@dataclass(frozen=True, slots=True)
class _FreshnessDrop:
    source: Literal["opus_input_stale", "opus_input_backpressure"]
    dropped_packets: int
    live_edge: _QueuedAudio | None
    backlog_qsize: int


class _AudioQueuePort(AudioInputPort):
    def __init__(self, owner: RvaWssConnection, capacity: int) -> None:
        self._owner = owner
        self.queue: asyncio.Queue[_QueuedAudio | None] = asyncio.Queue(capacity)
        self._closed = False
        self._closed_event = asyncio.Event()
        self._consumer_timeline_timestamp: int | None = None
        self._consumer_timeline_started_at = 0.0
        self._last_timestamp: int | None = None
        self._last_received_at = 0.0

    async def receive_audio(self, packet: InboundAudioPacket) -> None:
        if self._closed:
            raise RvaRuntimeError("audio input is closed")
        received_at = self._owner._clock()  # noqa: SLF001
        if self._consumer_timeline_timestamp is None:
            self._consumer_timeline_timestamp = packet.timestamp
            self._consumer_timeline_started_at = received_at
        if self._last_timestamp is not None:
            timestamp_delta = (packet.timestamp - self._last_timestamp) & 0xFFFFFFFF
            arrival_delta = max(0.0, received_at - self._last_received_at)
            media_delta = timestamp_delta / SAMPLE_RATE
            if (
                timestamp_delta == 0
                or timestamp_delta % SAMPLES_PER_PACKET != 0
                or media_delta > arrival_delta + self._owner._limits.uplink_max_age_seconds  # noqa: SLF001
            ):
                raise RvaBindingError("invalid_media_timestamp")
        self._last_timestamp = packet.timestamp
        self._last_received_at = received_at
        timestamp_delta = (packet.timestamp - self._consumer_timeline_timestamp) & 0xFFFFFFFF
        expected_at = self._consumer_timeline_started_at + timestamp_delta / SAMPLE_RATE
        max_age = self._owner._limits.uplink_max_age_seconds  # noqa: SLF001
        if expected_at > received_at + max_age:
            raise RvaBindingError("invalid_media_timestamp")
        queued = _QueuedAudio(
            packet=packet,
            received_at=received_at,
            expected_at=expected_at,
            deadline_at=min(received_at, expected_at) + max_age,
        )
        try:
            self.queue.put_nowait(queued)
        except asyncio.QueueFull:
            put_task = asyncio.create_task(self.queue.put(queued), name="rva-opus-input-put")
            close_task = asyncio.create_task(self._closed_event.wait(), name="rva-opus-input-close-wait")
            try:
                done, _ = await asyncio.wait(
                    {put_task, close_task},
                    timeout=self._owner._limits.queue_timeout_seconds,  # noqa: SLF001
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if close_task in done or self._closed:
                    return
                if put_task not in done:
                    raise RvaOverloadedError(
                        "input media queue is full",
                        source="opus_input",
                        qsize=self.queue.qsize(),
                        capacity=self.queue.maxsize,
                    )
                await put_task
            finally:
                for task in (put_task, close_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(put_task, close_task, return_exceptions=True)

    def mark_consumed(self, queued: _QueuedAudio) -> None:
        if self._consumer_timeline_timestamp is None:
            return
        timestamp_delta = (queued.packet.timestamp - self._consumer_timeline_timestamp) & 0xFFFFFFFF
        media_elapsed = timestamp_delta / SAMPLE_RATE
        if media_elapsed >= _TIMELINE_REANCHOR_SECONDS:
            phase_error = queued.received_at - queued.expected_at
            max_correction = media_elapsed * _TIMELINE_MAX_SKEW_RATIO
            correction = max(-max_correction, min(max_correction, phase_error))
            self._consumer_timeline_timestamp = queued.packet.timestamp
            self._consumer_timeline_started_at = queued.expected_at + correction

    def drop_stale_to_live_edge(self, current: _QueuedAudio, *, now: float) -> _FreshnessDrop:
        backlog_qsize = self.queue.qsize()
        pending: list[_QueuedAudio] = []
        closed_sentinel = False
        while True:
            try:
                item = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item is None:
                closed_sentinel = True
                break
            pending.append(item)

        candidates = [current, *pending]
        fresh = [item for item in candidates if item.deadline_at > now]
        live_edge = fresh[-1] if fresh and not closed_sentinel else None
        stale_count = sum(item.deadline_at <= now for item in candidates)
        isolated_stale = (
            stale_count == 1
            and live_edge is not None
            and backlog_qsize < self.queue.maxsize
        )
        source: Literal["opus_input_stale", "opus_input_backpressure"] = (
            "opus_input_stale" if isolated_stale else "opus_input_backpressure"
        )
        if live_edge is not None:
            live_edge = _QueuedAudio(
                packet=live_edge.packet,
                received_at=live_edge.received_at,
                expected_at=live_edge.received_at,
                deadline_at=live_edge.received_at + self._owner._limits.uplink_max_age_seconds,  # noqa: SLF001
            )
            self._consumer_timeline_timestamp = live_edge.packet.timestamp
            self._consumer_timeline_started_at = live_edge.received_at
            self.queue.put_nowait(live_edge)
        if closed_sentinel:
            self.queue.put_nowait(None)
        return _FreshnessDrop(
            source=source,
            dropped_packets=len(candidates) - (1 if live_edge is not None else 0),
            live_edge=live_edge,
            backlog_qsize=backlog_qsize,
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._closed_event.set()
        while not self.queue.empty():
            self.queue.get_nowait()
        self.queue.put_nowait(None)


class _AgentPort(AgentControlPort):
    def __init__(self, owner: RvaWssConnection) -> None:
        self._owner = owner
        self._closed = False

    async def interrupt(self, target: PlaybackRef) -> None:
        del target
        active_epoch = self._owner._active_producer_epoch  # noqa: SLF001
        if active_epoch is not None:
            self._owner._minimum_producer_epoch = max(  # noqa: SLF001
                self._owner._minimum_producer_epoch,  # noqa: SLF001
                active_epoch + 1,
            )
        self._owner._generation_changed.set()  # noqa: SLF001
        runner = self._owner._runner  # noqa: SLF001
        if runner is None:
            raise RvaRuntimeError("agent runner is unavailable")
        try:
            producer_epoch = await asyncio.wait_for(
                runner.interrupt(),
                timeout=self._owner._limits.runner_timeout_seconds,  # noqa: SLF001
            )
        except TimeoutError as exc:
            raise RvaRuntimeError("agent interrupt timed out") from exc
        self._owner._minimum_producer_epoch = max(  # noqa: SLF001
            self._owner._minimum_producer_epoch,  # noqa: SLF001
            producer_epoch,
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._owner._close_runner()  # noqa: SLF001


class RvaWssConnection:
    """Own one socket, codec, runner and every child task for an RVA session."""

    def __init__(
        self,
        websocket: WebSocket,
        *,
        expected_device_id: str,
        session_id: str,
        session_epoch: str,
        media_id: bytes,
        media_epoch: int,
        allowed_profiles: frozenset[str] = frozenset({WSS_PROFILE}),
        udp_gateway: UdpMediaGateway | None = None,
        runner_factory: RunnerFactory,
        limits: RvaRuntimeLimits | None = None,
        codec_factory: CodecFactory = RvaOpusCodec,
        clock: Clock = time.monotonic,
        wall_clock: Clock = time.time,
        interruption_policy: LayeredInterruptionPolicy | None = None,
    ) -> None:
        self._websocket = websocket
        self._limits = limits or RvaRuntimeLimits()
        self._interruption_policy = interruption_policy
        self._interruption_coordinator = (
            InterruptionCoordinator(interruption_policy, self._accept_interruption)
            if interruption_policy is not None
            else None
        )
        self._runner_factory = runner_factory
        self._codec_factory = codec_factory
        self._clock = clock
        self._wall_clock = wall_clock
        self._failures: asyncio.Queue[BaseException] = asyncio.Queue(1)
        self._audio_port = _AudioQueuePort(self, self._limits.input_queue_packets)
        self._agent_port = _AgentPort(self)
        self._udp_uplink_sequence = 0
        self._udp_session: UdpMediaSession | None = None
        if UDP_PROFILE in allowed_profiles and udp_gateway is not None:
            self._udp_session = udp_gateway.create_session(self._receive_udp_audio, self._report_failure)
            media_id = self._udp_session.grant.media_id
            media_epoch = self._udp_session.grant.media_epoch
        self._binding = RvaWssBinding(
            expected_device_id=expected_device_id,
            session_id=session_id,
            session_epoch=session_epoch,
            media_id=media_id,
            media_epoch=media_epoch,
            allowed_profiles=allowed_profiles,
            udp_grant=self._rva_udp_grant(),
            audio_port=self._audio_port,
            agent_port=self._agent_port,
            close_stage_timeout_seconds=self._limits.agent_close_stage_timeout_seconds,
        )
        self._codec: RvaOpusCodec | None = None
        self._runner: AgentRunner | None = None
        self._output: asyncio.Queue[_Outbound] = asyncio.Queue(self._limits.output_queue_items)
        self._segments: asyncio.Queue[_Outbound] = asyncio.Queue(self._limits.output_queue_items)
        self._tasks: set[asyncio.Task[None]] = set()
        self._aux_tasks: set[asyncio.Task[None]] = set()
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False
        self._minimum_producer_epoch = 1
        self._active_producer_epoch: int | None = None
        self._generation_changed = asyncio.Event()
        self._activity_changed = asyncio.Event()
        self._wire_lock = asyncio.Lock()
        self._last_activity = self._clock()
        self._last_udp_authenticated = 0
        self._input_pcm_sequence = 0
        self._downlink_timestamp = 0
        self._wss_input_packets = 0
        self._udp_input_packets = 0
        self._decoded_pcm_frames = 0
        self._invalid_opus_packets = 0
        self._consecutive_invalid_opus_packets = 0
        self._runner_push_frames = 0
        self._downlink_packets = 0
        self._response_sequence = 0
        self._utterance_sequence = 0
        self._active_utterance_id: str | None = None
        self._transcript_sequence = 0
        self._pending_assistant_text: str | None = None
        self._assistant_text_sent = ""
        self._active_assistant_text = ""
        self._playback_started_at: float | None = None
        self._interrupt_candidate_started_at: float | None = None
        self._response_text_sequence = 0
        self.close_code: int | None = None
        self.close_reason: str | None = None
        self._overload_source = "none"
        self._overload_qsize = -1
        self._overload_capacity = -1
        self._overload_media_age_ms = -1
        self._overload_dropped_packets = 0
        self._overload_fresh_packet_available: bool | None = None
        self._primary_close_cause: tuple[int, str] | None = None

    @property
    def binding(self) -> RvaWssBinding:
        return self._binding

    async def run(self) -> None:
        close_code, close_reason = 1_000, "normal"
        try:
            first = await self._receive_first_control()
            opened_effect = await self._binding.receive_control(first)
            if len(opened_effect.outbound) != 1:
                raise RvaBindingError("expected_session_open")
            opened = opened_effect.outbound[0]
            self._mark_activity()
            logger.info(
                "rva_session_opened session=%s session_epoch=%s selected_media_profile=%s udp_enabled=%s",
                self._binding_id,
                self._binding.session_epoch,
                self._binding.selected_media_profile,
                self._binding.selected_media_profile == UDP_PROFILE,
            )
            self._codec = await asyncio.to_thread(self._codec_factory)
            self._runner = self._runner_factory(self._emit_segment, self._request_response_end)
            configure_text_sinks = getattr(self._runner, "set_text_sinks", None)
            if callable(configure_text_sinks):
                configure_text_sinks(self._emit_user_transcript, self._emit_assistant_text)

            handshake_ack = asyncio.get_running_loop().create_future()
            self._output.put_nowait(_Outbound("control", opened, handshake_ack))
            writer = asyncio.create_task(self._writer_loop(), name=f"rva-writer-{self._binding_id}")
            self._tasks.add(writer)
            done, _ = await asyncio.wait(
                {writer, handshake_ack},
                timeout=self._limits.handshake_timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise RvaHandshakeTimeoutError("session.opened acknowledgement timed out")
            if writer in done:
                try:
                    await writer
                except RvaControlTimeoutError as exc:
                    raise RvaHandshakeTimeoutError("session.opened send timed out") from exc
                raise RvaRuntimeError("writer stopped during handshake")
            await handshake_ack
            try:
                await asyncio.wait_for(self._runner.start(), timeout=self._limits.runner_timeout_seconds)
            except TimeoutError as exc:
                raise RvaRuntimeStartTimeoutError("agent runner start timed out") from exc
            self._tasks.update(
                {
                    asyncio.create_task(self._reader_loop(), name=f"rva-reader-{self._binding_id}"),
                    asyncio.create_task(self._input_loop(), name=f"rva-input-{self._binding_id}"),
                    asyncio.create_task(self._segment_loop(), name=f"rva-segments-{self._binding_id}"),
                    asyncio.create_task(self._failure_loop(), name=f"rva-failure-{self._binding_id}"),
                    asyncio.create_task(
                        self._runner_terminal_loop(),
                        name=f"rva-runner-terminal-{self._binding_id}",
                    ),
                    asyncio.create_task(self._idle_loop(), name=f"rva-idle-{self._binding_id}"),
                }
            )
            if self._binding.selected_media_profile == UDP_PROFILE:
                udp_session = self._udp_session
                if udp_session is None:
                    raise RvaRuntimeError("UDP media session is unavailable")
                await udp_session.wait_ready(udp_session.grant.probe_timeout_ms / 1_000)
            elif self._udp_session is not None:
                await self._udp_session.close()
                self._udp_session = None
            done, _ = await asyncio.wait(self._tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task.cancelled():
                    if self._closed:
                        return
                    raise RvaRuntimeError(f"child task cancelled unexpectedly: {task.get_name()}")
                exception = task.exception()
                if exception is not None:
                    raise exception
        except WebSocketDisconnect:
            pass
        except RvaHandshakeTimeoutError:
            close_code, close_reason = 1_008, "handshake_timeout"
        except RvaControlTimeoutError:
            close_code, close_reason = 1_011, "control_timeout"
        except RvaMediaSendTimeoutError:
            close_code, close_reason = 1_011, "media_send_timeout"
        except RvaRuntimeStartTimeoutError:
            close_code, close_reason = 1_011, "runtime_start_timeout"
        except RvaAgentRuntimeFailedError:
            logger.warning("rva_agent_runtime_failed session=%s", self._binding_id)
            close_code, close_reason = 1_011, "runtime_failure"
        except UdpProbeTimeoutError:
            close_code, close_reason = 1_008, "udp_probe_timeout"
        except UdpGrantExpiredError:
            close_code, close_reason = 1_000, "udp_grant_expired"
        except RvaMessageTooLarge:
            close_code, close_reason = 1_009, "message_too_large"
        except RvaOverloadedError as exc:
            self._overload_source = exc.source
            self._overload_qsize = exc.qsize
            self._overload_capacity = exc.capacity
            self._overload_media_age_ms = exc.media_age_ms
            self._overload_dropped_packets = exc.dropped_packets
            self._overload_fresh_packet_available = exc.fresh_packet_available
            close_code, close_reason = 1_013, "media_overloaded"
            if exc.source in {"opus_input_stale", "opus_input_backpressure"}:
                self._primary_close_cause = (close_code, close_reason)
                await self._send_session_error_best_effort(
                    code="media_overloaded",
                    message="Media freshness budget exceeded; reconnect required",
                )
        except RvaIdleTimeoutError:
            close_code, close_reason = 1_000, "idle_timeout"
        except RvaMediaDecodeError:
            close_code, close_reason = 1_011, "media_decode_failed"
        except RvaBindingError as exc:
            logger.warning(
                "rva_protocol_error session=%s error_code=%s",
                self._binding_id,
                exc.code,
            )
            close_code, close_reason = 1_002, "protocol_error"
        except Exception:
            logger.exception("RVA WSS session failed session=%s", self._binding_id)
            close_code, close_reason = 1_011, "runtime_failure"
        finally:
            if self._primary_close_cause is not None:
                close_code, close_reason = self._primary_close_cause
            await self.close(code=close_code, reason=close_reason)

    async def close(self, *, code: int = 1_000, reason: str = "normal") -> None:
        if self._close_task is None:
            self._closed = True
            self.close_code = code
            self.close_reason = reason
            logger.info(
                "rva_session_closing session=%s session_epoch=%s close_code=%d close_reason=%s "
                "selected_media_profile=%s wss_input_packets=%d udp_input_packets=%d "
                "decoded_pcm_frames=%d invalid_opus_packets=%d runner_push_frames=%d downlink_packets=%d "
                "overload_source=%s overload_qsize=%d overload_capacity=%d overload_media_age_ms=%d "
                "overload_dropped_packets=%d overload_fresh_packet_available=%s",
                self._binding_id,
                self._binding.session_epoch,
                code,
                reason,
                self._binding.selected_media_profile or "none",
                self._wss_input_packets,
                self._udp_input_packets,
                self._decoded_pcm_frames,
                self._invalid_opus_packets,
                self._runner_push_frames,
                self._downlink_packets,
                self._overload_source,
                self._overload_qsize,
                self._overload_capacity,
                self._overload_media_age_ms,
                self._overload_dropped_packets,
                (
                    "unknown"
                    if self._overload_fresh_packet_available is None
                    else str(self._overload_fresh_packet_available).lower()
                ),
            )
            self._close_task = asyncio.create_task(self._close_impl(code, reason), name=f"rva-close-{self._binding_id}")
        try:
            await asyncio.wait_for(asyncio.shield(self._close_task), timeout=self._limits.close_timeout_seconds)
        except asyncio.CancelledError:
            self._close_task.cancel()
            await asyncio.gather(self._close_task, return_exceptions=True)
            raise
        except TimeoutError:
            logger.error("RVA bounded close timed out session=%s", self._binding_id)

    async def wait_closed(self) -> None:
        """Wait for owned cleanup after the bounded caller-facing close window."""

        task = self._close_task
        if task is None:
            return
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def _receive_first_control(self) -> str:
        try:
            async with asyncio.timeout(self._limits.handshake_timeout_seconds):
                frame = await self._websocket.receive()
        except TimeoutError as exc:
            raise RvaHandshakeTimeoutError("session.open was not received") from exc
        if frame.get("type") == "websocket.disconnect":
            raise WebSocketDisconnect(code=int(frame.get("code", 1_000)))
        text = frame.get("text")
        if not isinstance(text, str):
            raise RvaBindingError("first_frame_must_be_session_open")
        return text

    async def _reader_loop(self) -> None:
        while True:
            frame = await self._websocket.receive()
            if frame.get("type") == "websocket.disconnect":
                return
            text = frame.get("text")
            binary = frame.get("bytes")
            if isinstance(text, str):
                effect = await self._binding.receive_control(text)
                self._mark_activity()
                await self._apply_control_effect(effect)
                if effect.close_after_send:
                    return
            elif isinstance(binary, bytes):
                await self._binding.receive_media(binary)
                self._wss_input_packets += 1
                self._log_input_progress_if_due(source="wss", packets=self._wss_input_packets)
                self._mark_activity()
            else:
                raise RvaBindingError("unsupported_websocket_frame")

    async def _apply_control_effect(self, effect: ControlEffect) -> None:
        if effect.interrupt is not None:
            self._minimum_producer_epoch = max(
                self._minimum_producer_epoch,
                effect.interrupt.producer_epoch + 1,
            )
            self._generation_changed.set()
        for event in effect.outbound:
            await self._send_control_serialized(event)
        runner = self._runner
        if effect.playback_started is not None and runner is not None:
            self._playback_started_at = self._clock()
            self._interrupt_candidate_started_at = None
            await runner.playback_started(time.time())
        if effect.playback_ended is not None and runner is not None:
            fact = effect.playback_ended
            await runner.playback_finished(
                fact.played_samples / SAMPLE_RATE,
                fact.outcome != "completed",
            )
            runner.set_response_gate(False)
            self._playback_started_at = None
            self._interrupt_candidate_started_at = None
            self._active_assistant_text = ""
        if effect.interrupt is not None:
            task = asyncio.create_task(
                self._interrupt_runner(effect.interrupt),
                name=f"rva-interrupt-{self._binding_id}",
            )
            self._aux_tasks.add(task)
            task.add_done_callback(self._on_aux_done)

    async def _input_loop(self) -> None:
        while True:
            queued = await self._audio_port.queue.get()
            if queued is None:
                return
            self._remaining_uplink_budget(queued)
            packet = queued.packet
            codec = self._codec
            runner = self._runner
            if codec is None or runner is None:
                raise RvaRuntimeError("runtime is not ready")
            try:
                frames = await asyncio.to_thread(
                    codec.decode_60ms,
                    packet.payload,
                    sequence_start=self._input_pcm_sequence,
                )
            except RvaOpusDecodeError as exc:
                self._invalid_opus_packets += 1
                self._consecutive_invalid_opus_packets += 1
                consecutive = self._consecutive_invalid_opus_packets
                if (
                    self._invalid_opus_packets == 1
                    or self._invalid_opus_packets % 50 == 0
                    or consecutive >= self._limits.max_consecutive_invalid_opus_packets
                ):
                    logger.warning(
                        "rva_opus_packet_dropped session=%s sequence=%d timestamp=%d payload_bytes=%d "
                        "consecutive=%d total=%d decoder_error=%s",
                        self._binding_id,
                        packet.sequence,
                        packet.timestamp,
                        len(packet.payload),
                        consecutive,
                        self._invalid_opus_packets,
                        type(exc).__name__,
                    )
                if consecutive >= self._limits.max_consecutive_invalid_opus_packets:
                    raise RvaMediaDecodeError("consecutive Opus decode failures") from exc
                continue
            self._consecutive_invalid_opus_packets = 0
            self._input_pcm_sequence += len(frames)
            self._decoded_pcm_frames += len(frames)
            for frame in frames:
                try:
                    remaining = self._remaining_uplink_budget(queued)
                    await asyncio.wait_for(
                        runner.push_audio(frame),
                        timeout=remaining,
                    )
                    self._remaining_uplink_budget(queued)
                    self._runner_push_frames += 1
                except TimeoutError as exc:
                    raise self._stale_uplink_error(queued) from exc
                except RoomlessAudioInputOverloadedError as exc:
                    raise RvaOverloadedError(
                        "agent input is full",
                        source=exc.source,
                        qsize=exc.qsize,
                        capacity=exc.capacity,
                    ) from exc
                except AgentRunnerTerminatedError as exc:
                    raise RvaAgentRuntimeFailedError("agent runner terminated") from exc
                except BufferError as exc:
                    raise RvaOverloadedError(
                        "agent input is full",
                        source="agent_input_unknown",
                        qsize=-1,
                        capacity=-1,
                    ) from exc
            self._audio_port.mark_consumed(queued)

    def _remaining_uplink_budget(self, queued: _QueuedAudio) -> float:
        remaining = queued.deadline_at - self._clock()
        if remaining <= 0:
            raise self._stale_uplink_error(queued)
        return remaining

    def _stale_uplink_error(self, queued: _QueuedAudio) -> RvaOverloadedError:
        now = self._clock()
        media_age_seconds = max(0.0, now - queued.received_at, now - queued.expected_at)
        dropped = self._audio_port.drop_stale_to_live_edge(queued, now=now)
        return RvaOverloadedError(
            "input media exceeded freshness budget",
            source=dropped.source,
            qsize=dropped.backlog_qsize,
            capacity=self._audio_port.queue.maxsize,
            media_age_ms=round(media_age_seconds * 1_000),
            dropped_packets=dropped.dropped_packets,
            fresh_packet_available=dropped.live_edge is not None,
        )

    async def _writer_loop(self) -> None:
        while True:
            item = await self._output.get()
            if item.kind != "control" or not isinstance(item.payload, str):
                raise RvaRuntimeError("non-control item entered control queue")
            try:
                async with self._wire_lock:
                    async with asyncio.timeout(self._limits.wire_send_timeout_seconds):
                        await self._websocket.send_text(item.payload)
            except TimeoutError as exc:
                raise RvaControlTimeoutError("control send timed out") from exc
            if item.ack is not None and not item.ack.done():
                item.ack.set_result(None)

    async def _runner_terminal_loop(self) -> None:
        runner = self._runner
        if runner is None:
            raise RvaRuntimeError("agent runner is unavailable")
        terminal = await asyncio.shield(runner.terminal)
        if terminal.kind is AgentRunnerTerminalKind.OWNER_CLOSED:
            return
        self._primary_close_cause = (1_011, "runtime_failure")
        await self._send_session_error_best_effort(
            code="agent_runtime_failed",
            message="Agent runtime terminated; reconnect required",
        )
        raise RvaAgentRuntimeFailedError("agent runner terminated unexpectedly")

    async def _send_session_error_best_effort(self, *, code: str, message: str) -> None:
        try:
            event = self._binding.session_error(
                code=code,
                retryable=True,
                message=message,
            )
            await self._send_control_serialized(event)
        except Exception as exc:
            logger.warning(
                "rva_session_error_notification_failed session=%s code=%s error_type=%s",
                self._binding_id,
                code,
                type(exc).__name__,
            )

    async def _segment_loop(self) -> None:
        while True:
            item = await self._segments.get()
            if item.kind == "segment" and isinstance(item.payload, AgentOutputSegment):
                await self._send_segment(item.payload, item.assistant_text)
            elif item.kind == "response_end" and isinstance(item.payload, int):
                await self._finish_response(item.payload)
            else:
                raise RvaRuntimeError("invalid item entered media queue")

    async def _send_segment(self, segment: AgentOutputSegment, assistant_text: str | None) -> None:
        if self._closed or segment.producer_epoch < self._minimum_producer_epoch or not segment.frames:
            logger.info(
                "rva_segment_skipped session=%s closed=%s segment_epoch=%d minimum_epoch=%d frames=%d",
                self._binding_id,
                self._closed,
                segment.producer_epoch,
                self._minimum_producer_epoch,
                len(segment.frames),
            )
            return
        active = self._binding.active_response
        if active is None:
            self._response_sequence += 1
            response_id = f"resp-{self._response_sequence:08d}"
        else:
            response_id = active.response_id
        record, begin = await self._binding.response_begin(
            response_id=response_id,
            producer_epoch=segment.producer_epoch,
        )
        if begin is not None:
            if self._runner is not None:
                self._runner.set_response_gate(True)
            await self._send_control_serialized(begin)
            self._assistant_text_sent = ""
            self._active_assistant_text = ""
            self._response_text_sequence = 0
        self._active_producer_epoch = segment.producer_epoch
        self._pending_assistant_text = self._pending_assistant_text or assistant_text
        self._active_assistant_text = self._pending_assistant_text or self._active_assistant_text
        await self._send_pending_assistant_text(record)
        segment_started_at = self._clock()
        interrupted = False
        interrupt_reason = "none"
        for packet_index, offset in enumerate(range(0, len(segment.frames), 3)):
            if segment.producer_epoch < self._minimum_producer_epoch or self._binding.active_response is not record:
                interrupted = True
                interrupt_reason = "producer_epoch_or_inactive_response"
                break
            await self._send_pending_assistant_text(record)
            frames = segment.frames[offset : offset + 3]
            codec = self._codec
            if codec is None:
                raise RvaRuntimeError("codec is unavailable")
            payload = await asyncio.to_thread(codec.encode_60ms, frames)
            if packet_index >= self._limits.playback_prebuffer_packets:
                deadline = segment_started_at + (
                    packet_index - self._limits.playback_prebuffer_packets + 1
                ) * (FRAME_DURATION_MS / 1_000)
                if not await self._wait_playback_deadline(deadline, record):
                    interrupted = True
                    interrupt_reason = "response_generation_changed"
                    break
            try:
                if self._binding.selected_media_profile == UDP_PROFILE:
                    udp_session = self._udp_session
                    if udp_session is None:
                        raise RvaRuntimeError("UDP media session is unavailable")
                    sequence = await udp_session.send_audio(
                        payload,
                        timestamp=self._downlink_timestamp,
                        generation=record.target.generation,
                    )
                    self._binding.note_downlink_media(record, sequence)
                else:
                    media = self._binding.serialize_audio(
                        payload,
                        timestamp=self._downlink_timestamp,
                        record=record,
                    )
                    try:
                        async with self._wire_lock:
                            async with asyncio.timeout(self._limits.wire_send_timeout_seconds):
                                await self._websocket.send_bytes(media)
                    except TimeoutError as exc:
                        raise RvaMediaSendTimeoutError("media send timed out") from exc
            except RvaBindingError as exc:
                if exc.code == "stale_generation":
                    interrupted = True
                    interrupt_reason = "stale_generation"
                    break
                raise
            self._downlink_packets += 1
            self._downlink_timestamp = (self._downlink_timestamp + SAMPLES_PER_PACKET) & 0xFFFFFFFF
        await self._send_pending_assistant_text(record)
        if interrupted:
            logger.info(
                "rva_segment_fenced session=%s response_id=%s reason=%s segment_epoch=%d minimum_epoch=%d",
                self._binding_id,
                record.response_id,
                interrupt_reason,
                segment.producer_epoch,
                self._minimum_producer_epoch,
            )

    async def _send_pending_assistant_text(self, record: ResponseRecord) -> None:
        current = self._pending_assistant_text
        if not current or self._binding.active_response is not record:
            return
        if current.startswith(self._assistant_text_sent):
            delta = current[len(self._assistant_text_sent) :]
        else:
            delta = current
        self._pending_assistant_text = None
        self._assistant_text_sent = current
        if not delta:
            return
        event = self._binding.response_text(
            record=record,
            sequence=self._response_text_sequence,
            text=delta,
        )
        self._response_text_sequence += 1
        await self._send_control_serialized(event)

    async def _wait_playback_deadline(self, deadline: float, record: ResponseRecord) -> bool:
        remaining = deadline - self._clock()
        if remaining <= 0:
            return self._binding.active_response is record
        self._generation_changed.clear()
        try:
            await asyncio.wait_for(self._generation_changed.wait(), timeout=remaining)
        except TimeoutError:
            return self._binding.active_response is record
        return False

    async def _emit_segment(self, segment: AgentOutputSegment) -> None:
        if self._closed or segment.producer_epoch < self._minimum_producer_epoch or not segment.frames:
            logger.info(
                "rva_emit_segment_dropped session=%s closed=%s segment_epoch=%d minimum_epoch=%d frames=%d",
                self._binding_id,
                self._closed,
                segment.producer_epoch,
                self._minimum_producer_epoch,
                len(segment.frames),
            )
            return
        if len(segment.frames) > self._limits.max_segment_frames:
            error = RvaOverloadedError(
                "agent output segment exceeds frame limit",
                source="agent_output_segment",
                qsize=len(segment.frames),
                capacity=self._limits.max_segment_frames,
            )
            self._report_failure(error)
            raise error
        assistant_text, self._pending_assistant_text = self._pending_assistant_text, None
        try:
            await self._enqueue_segment(_Outbound("segment", segment, assistant_text=assistant_text))
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._report_failure(exc)
            raise

    def _emit_user_transcript(self, text: str, is_final: bool) -> bool:
        if self._closed:
            return False
        overlapped_playback = self._binding.current_playback is not None
        try:
            self._submit_interruption_candidate(text, is_final)
            if self._active_utterance_id is None:
                self._utterance_sequence += 1
                self._active_utterance_id = f"utt-{self._utterance_sequence:08d}"
                self._transcript_sequence = 0
            if is_final:
                event = self._binding.transcript_final(
                    utterance_id=self._active_utterance_id,
                    sequence=self._transcript_sequence,
                    text=text,
                )
                self._active_utterance_id = None
                self._transcript_sequence = 0
            else:
                event = self._binding.transcript_delta(
                    utterance_id=self._active_utterance_id,
                    sequence=self._transcript_sequence,
                    text=text,
                )
                self._transcript_sequence += 1
            self._enqueue_nowait(_Outbound("control", event))
        except BaseException as exc:
            self._report_failure(exc)
        return overlapped_playback

    def _submit_interruption_candidate(self, text: str, is_final: bool) -> None:
        coordinator = self._interruption_coordinator
        record = self._binding.current_playback
        if coordinator is None or record is None:
            return
        playback_started_at = self._playback_started_at
        if playback_started_at is None:
            return
        if self._interrupt_candidate_started_at is None:
            self._interrupt_candidate_started_at = self._clock()
        now = self._clock()
        coordinator.submit(
            InterruptionContext(
                transcript=text,
                is_final=is_final,
                playback_age_seconds=max(0.0, now - playback_started_at),
                candidate_age_seconds=max(0.0, now - self._interrupt_candidate_started_at),
                assistant_text=self._active_assistant_text,
                response_id=record.response_id,
                generation=record.target.generation,
            )
        )

    async def _accept_interruption(self, context: InterruptionContext) -> None:
        record = self._binding.current_playback
        if (
            record is None
            or record.response_id != context.response_id
            or record.target.generation != context.generation
        ):
            return
        effect = await self._binding.cancel_active_response(cause="recognized_interrupt")
        await self._apply_control_effect(effect)

    def _emit_assistant_text(self, text: str) -> None:
        if not self._closed and text:
            self._pending_assistant_text = text

    def _request_response_end(self, producer_epoch: int) -> None:
        if self._closed:
            return
        task = asyncio.create_task(
            self._enqueue_segment(_Outbound("response_end", producer_epoch)),
            name=f"rva-response-end-{self._binding_id}",
        )
        self._aux_tasks.add(task)
        task.add_done_callback(self._on_aux_done)

    async def _finish_response(self, producer_epoch: int) -> None:
        record = self._binding.active_response
        if record is None or record.producer_epoch != producer_epoch:
            return
        try:
            await self._send_pending_assistant_text(record)
            event = await self._binding.response_end(record=record)
        except RvaBindingError as exc:
            if exc.code != "stale_generation":
                raise
            return
        await self._send_control_serialized(event)
        if self._active_producer_epoch == producer_epoch:
            self._active_producer_epoch = None

    async def _interrupt_runner(self, record: ResponseRecord) -> None:
        await self._agent_port.interrupt(record.target)

    def _on_aux_done(self, task: asyncio.Task[None]) -> None:
        self._aux_tasks.discard(task)
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:
            self._report_failure(exception)

    async def _enqueue(self, item: _Outbound) -> None:
        try:
            await asyncio.wait_for(self._output.put(item), timeout=self._limits.queue_timeout_seconds)
        except TimeoutError as exc:
            raise RvaOverloadedError(
                "output queue is full",
                source="control_output",
                qsize=self._output.qsize(),
                capacity=self._output.maxsize,
            ) from exc

    async def _send_control_serialized(self, payload: str) -> None:
        ack = asyncio.get_running_loop().create_future()
        await self._enqueue(_Outbound("control", payload, ack))
        try:
            await asyncio.wait_for(
                ack,
                timeout=self._limits.queue_timeout_seconds + self._limits.wire_send_timeout_seconds,
            )
        except TimeoutError as exc:
            raise RvaControlTimeoutError("control acknowledgement timed out") from exc

    async def _enqueue_segment(self, item: _Outbound) -> None:
        try:
            await asyncio.wait_for(self._segments.put(item), timeout=self._limits.queue_timeout_seconds)
        except TimeoutError as exc:
            raise RvaOverloadedError(
                "segment queue is full",
                source="segment_output",
                qsize=self._segments.qsize(),
                capacity=self._segments.maxsize,
            ) from exc

    def _enqueue_nowait(self, item: _Outbound) -> None:
        try:
            self._output.put_nowait(item)
        except asyncio.QueueFull as exc:
            raise RvaOverloadedError(
                "output queue is full",
                source="control_output",
                qsize=self._output.qsize(),
                capacity=self._output.maxsize,
            ) from exc

    def _report_failure(self, error: BaseException) -> None:
        if self._failures.empty():
            self._failures.put_nowait(error)

    async def _receive_udp_audio(self, payload: bytes, timestamp: int, generation: int) -> None:
        self._mark_activity()
        if generation != self._binding.uplink_generation:
            return
        packet = InboundAudioPacket(self._udp_uplink_sequence, timestamp, payload)
        self._udp_uplink_sequence += 1
        self._udp_input_packets += 1
        self._log_input_progress_if_due(source="udp", packets=self._udp_input_packets)
        await self._audio_port.receive_audio(packet)

    def _log_input_progress_if_due(self, *, source: str, packets: int) -> None:
        if packets == 1 or packets % 50 == 0:
            logger.info(
                "rva_media_input session=%s source=%s packets=%d decoded_pcm_frames=%d "
                "runner_push_frames=%d queue_size=%d",
                self._binding_id,
                source,
                packets,
                self._decoded_pcm_frames,
                self._runner_push_frames,
                self._audio_port.queue.qsize(),
            )

    def _rva_udp_grant(self) -> dict[str, object] | None:
        session = self._udp_session
        if session is None:
            return None
        grant = session.grant
        refresh_after_ms = max(
            1_000,
            min(3_600_000, grant.expires_at * 1_000 - int(self._wall_clock() * 1_000) - 5_000),
        )
        return {
            "host": grant.host,
            "port": grant.port,
            "expires_at_ms": grant.expires_at * 1_000,
            "refresh_after_ms": refresh_after_ms,
            "uplink_key_b64": base64.b64encode(grant.uplink_key).decode("ascii"),
            "uplink_salt_b64": base64.b64encode(grant.uplink_salt).decode("ascii"),
            "downlink_key_b64": base64.b64encode(grant.downlink_key).decode("ascii"),
            "downlink_salt_b64": base64.b64encode(grant.downlink_salt).decode("ascii"),
            "probe_timeout_ms": grant.probe_timeout_ms,
        }

    async def _failure_loop(self) -> None:
        raise await self._failures.get()

    async def _idle_loop(self) -> None:
        poll_interval = min(1.0, self._limits.idle_timeout_seconds / 4)
        while True:
            udp_session = self._udp_session
            if udp_session is not None and udp_session.stats.authenticated != self._last_udp_authenticated:
                self._last_udp_authenticated = udp_session.stats.authenticated
                self._mark_activity()
            remaining = self._last_activity + self._limits.idle_timeout_seconds - self._clock()
            if remaining <= 0:
                raise RvaIdleTimeoutError("session idle timeout")
            self._activity_changed.clear()
            try:
                await asyncio.wait_for(self._activity_changed.wait(), timeout=min(remaining, poll_interval))
            except TimeoutError:
                pass

    def _mark_activity(self) -> None:
        self._last_activity = self._clock()
        self._activity_changed.set()

    async def _close_runner(self) -> None:
        runner, self._runner = self._runner, None
        if runner is None:
            return
        # VoiceSessionState owns the single bounded deadline for every close
        # port. A nested deadline here would orphan runner.close() when the
        # outer stage expires first.
        await runner.close()

    async def _close_impl(self, code: int, reason: str) -> None:
        current = asyncio.current_task()
        owned = {task for task in self._tasks | self._aux_tasks if task is not current}
        for task in owned:
            task.cancel()
        if owned:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*owned, return_exceptions=True),
                    timeout=self._limits.close_timeout_seconds / 2,
                )
        self._tasks.clear()
        self._aux_tasks.clear()
        if self._interruption_coordinator is not None:
            await self._interruption_coordinator.close()
        with contextlib.suppress(Exception):
            await self._binding.close()
        if self._udp_session is not None:
            with contextlib.suppress(Exception):
                await self._udp_session.close()
            self._udp_session = None
        try:
            await asyncio.wait_for(
                self._websocket.close(code=code, reason=reason),
                timeout=self._limits.close_timeout_seconds / 2,
            )
        except (RuntimeError, TimeoutError, WebSocketDisconnect):
            pass

    @property
    def _binding_id(self) -> str:
        return self._binding.session_id


__all__ = [
    "CodecFactory",
    "Clock",
    "RunnerFactory",
    "RvaOverloadedError",
    "RvaRuntimeError",
    "RvaRuntimeLimits",
    "RvaWssConnection",
]
