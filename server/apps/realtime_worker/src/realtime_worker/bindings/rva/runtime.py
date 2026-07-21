"""FastAPI WebSocket owner for the RVA WSS vertical runtime."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from realtime_worker.agent import AgentOutputSegment, AgentRunner
from realtime_worker.audio import PCM_SAMPLES
from realtime_worker.transport import UdpMediaGateway, UdpMediaSession
from realtime_worker.voice.session import PlaybackRef

from .binding import AgentControlPort, AudioInputPort, InboundAudioPacket, RvaWssBinding
from .codec import FRAME_DURATION_MS, SAMPLE_RATE, SAMPLES_PER_PACKET, RvaOpusCodec
from .protocol import UDP_PROFILE, WSS_PROFILE, RvaBindingError, RvaMessageTooLarge

logger = logging.getLogger(__name__)

RunnerFactory = Callable[[Callable[[AgentOutputSegment], Awaitable[None]], Callable[[int], None]], AgentRunner]
CodecFactory = Callable[[], RvaOpusCodec]
Clock = Callable[[], float]


class TextAwareRunner(Protocol):
    def set_text_sinks(
        self,
        user_transcript: Callable[[str, bool], None],
        assistant_text: Callable[[str], None],
    ) -> None: ...


class RvaRuntimeError(RuntimeError):
    pass


class RvaOverloadedError(RvaRuntimeError):
    pass


class RvaIdleTimeoutError(RvaRuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RvaRuntimeLimits:
    input_queue_packets: int = 8
    output_queue_items: int = 12
    max_segment_frames: int = 1_500
    queue_timeout_seconds: float = 0.2
    handshake_timeout_seconds: float = 5.0
    runner_timeout_seconds: float = 5.0
    close_timeout_seconds: float = 5.0
    idle_timeout_seconds: float = 45.0
    playback_prebuffer_packets: int = 4

    def __post_init__(self) -> None:
        positive = (
            self.input_queue_packets,
            self.output_queue_items,
            self.max_segment_frames,
            self.queue_timeout_seconds,
            self.handshake_timeout_seconds,
            self.runner_timeout_seconds,
            self.close_timeout_seconds,
            self.idle_timeout_seconds,
        )
        if any(value <= 0 for value in positive) or self.playback_prebuffer_packets < 0:
            raise ValueError("RVA runtime limits must be positive")


@dataclass(slots=True)
class _Outbound:
    kind: Literal["control", "segment"]
    payload: str | AgentOutputSegment
    ack: asyncio.Future[None] | None = None
    assistant_text: str | None = None


class _AudioQueuePort(AudioInputPort):
    def __init__(self, owner: RvaWssConnection, capacity: int) -> None:
        self._owner = owner
        self.queue: asyncio.Queue[InboundAudioPacket | None] = asyncio.Queue(capacity)
        self._closed = False

    async def receive_audio(self, packet: InboundAudioPacket) -> None:
        if self._closed:
            raise RvaRuntimeError("audio input is closed")
        try:
            self.queue.put_nowait(packet)
        except asyncio.QueueFull as exc:
            raise RvaOverloadedError("input media queue is full") from exc

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(asyncio.QueueFull):
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
    ) -> None:
        self._websocket = websocket
        self._limits = limits or RvaRuntimeLimits()
        self._runner_factory = runner_factory
        self._codec_factory = codec_factory
        self._clock = clock
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
        self._last_activity = self._clock()
        self._last_udp_authenticated = 0
        self._input_pcm_sequence = 0
        self._downlink_timestamp = 0
        self._response_sequence = 0
        self._utterance_sequence = 0
        self._active_utterance_id: str | None = None
        self._transcript_sequence = 0
        self._pending_assistant_text: str | None = None
        self._assistant_text_sent = ""
        self._response_text_sequence = 0
        self.close_code: int | None = None
        self.close_reason: str | None = None

    @property
    def binding(self) -> RvaWssBinding:
        return self._binding

    async def run(self) -> None:
        close_code, close_reason = 1_000, "normal"
        try:
            first = await self._receive_first_control()
            opened = await self._binding.receive_control(first)
            if opened is None:
                raise RvaBindingError("expected_session_open")
            self._mark_activity()
            self._codec = await asyncio.to_thread(self._codec_factory)
            self._runner = self._runner_factory(self._emit_segment, self._request_stop)
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
                raise TimeoutError
            if writer in done:
                await writer
                raise RvaRuntimeError("writer stopped during handshake")
            await handshake_ack
            await asyncio.wait_for(self._runner.start(), timeout=self._limits.runner_timeout_seconds)
            self._tasks.update(
                {
                    asyncio.create_task(self._reader_loop(), name=f"rva-reader-{self._binding_id}"),
                    asyncio.create_task(self._input_loop(), name=f"rva-input-{self._binding_id}"),
                    asyncio.create_task(self._segment_loop(), name=f"rva-segments-{self._binding_id}"),
                    asyncio.create_task(self._failure_loop(), name=f"rva-failure-{self._binding_id}"),
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
        except TimeoutError:
            close_code, close_reason = 1_008, "handshake_timeout"
        except RvaMessageTooLarge:
            close_code, close_reason = 1_009, "message_too_large"
        except RvaOverloadedError:
            close_code, close_reason = 1_013, "media_overloaded"
        except RvaIdleTimeoutError:
            close_code, close_reason = 1_000, "idle_timeout"
        except RvaBindingError:
            close_code, close_reason = 1_002, "protocol_error"
        except Exception:
            logger.exception("RVA WSS session failed session=%s", self._binding_id)
            close_code, close_reason = 1_011, "runtime_failure"
        finally:
            await self.close(code=close_code, reason=close_reason)

    async def close(self, *, code: int = 1_000, reason: str = "normal") -> None:
        if self._close_task is None:
            self._closed = True
            self.close_code = code
            self.close_reason = reason
            self._close_task = asyncio.create_task(self._close_impl(code, reason), name=f"rva-close-{self._binding_id}")
        try:
            await asyncio.wait_for(asyncio.shield(self._close_task), timeout=self._limits.close_timeout_seconds)
        except TimeoutError:
            logger.error("RVA bounded close timed out session=%s", self._binding_id)

    async def wait_closed(self) -> None:
        """Wait for owned cleanup after the bounded caller-facing close window."""

        task = self._close_task
        if task is None:
            return
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        await task
        if cancelled:
            raise asyncio.CancelledError

    async def _receive_first_control(self) -> str:
        async with asyncio.timeout(self._limits.handshake_timeout_seconds):
            frame = await self._websocket.receive()
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
                response = await self._binding.receive_control(text)
                self._mark_activity()
                if response is not None:
                    terminal = json.loads(response).get("type") == "session.close"
                    ack = asyncio.get_running_loop().create_future() if terminal else None
                    await self._enqueue(_Outbound("control", response, ack))
                    if ack is not None:
                        await ack
                        return
            elif isinstance(binary, bytes):
                await self._binding.receive_media(binary)
                self._mark_activity()
            else:
                raise RvaBindingError("unsupported_websocket_frame")

    async def _input_loop(self) -> None:
        while True:
            packet = await self._audio_port.queue.get()
            if packet is None:
                return
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
            except Exception as exc:
                raise RvaBindingError("invalid_opus_packet") from exc
            self._input_pcm_sequence += len(frames)
            for frame in frames:
                try:
                    await runner.push_audio(frame)
                except BufferError as exc:
                    raise RvaOverloadedError("agent input is full") from exc

    async def _writer_loop(self) -> None:
        while True:
            item = await self._output.get()
            if item.kind != "control" or not isinstance(item.payload, str):
                raise RvaRuntimeError("non-control item entered control queue")
            await self._websocket.send_text(item.payload)
            if item.ack is not None and not item.ack.done():
                item.ack.set_result(None)

    async def _segment_loop(self) -> None:
        while True:
            item = await self._segments.get()
            if item.kind != "segment" or not isinstance(item.payload, AgentOutputSegment):
                raise RvaRuntimeError("non-segment item entered media queue")
            await self._send_segment(item.payload, item.assistant_text)

    async def _send_segment(self, segment: AgentOutputSegment, assistant_text: str | None) -> None:
        if self._closed or segment.producer_epoch < self._minimum_producer_epoch or not segment.frames:
            return
        self._active_producer_epoch = segment.producer_epoch
        self._response_sequence += 1
        response_id = f"resp-{self._response_sequence:08d}"
        target, begin = await self._binding.response_begin(response_id=response_id)
        await self._send_control_serialized(begin)
        runner = self._runner
        if runner is None:
            raise RvaRuntimeError("agent runner is unavailable")
        self._pending_assistant_text = self._pending_assistant_text or assistant_text
        self._assistant_text_sent = ""
        self._response_text_sequence = 0
        await self._send_pending_assistant_text(response_id, target)
        playback_started_at = self._clock()
        await runner.playback_started(playback_started_at)
        played_samples = 0
        interrupted = False
        try:
            for packet_index, offset in enumerate(range(0, len(segment.frames), 3)):
                if segment.producer_epoch < self._minimum_producer_epoch or target != self._binding.active_playback:
                    interrupted = True
                    break
                await self._send_pending_assistant_text(response_id, target)
                frames = segment.frames[offset : offset + 3]
                codec = self._codec
                if codec is None:
                    raise RvaRuntimeError("codec is unavailable")
                payload = await asyncio.to_thread(codec.encode_60ms, frames)
                if packet_index >= self._limits.playback_prebuffer_packets:
                    deadline = playback_started_at + (
                        packet_index - self._limits.playback_prebuffer_packets + 1
                    ) * (FRAME_DURATION_MS / 1_000)
                    if not await self._wait_playback_deadline(deadline, target):
                        interrupted = True
                        break
                if self._binding.selected_media_profile == UDP_PROFILE:
                    udp_session = self._udp_session
                    if udp_session is None:
                        raise RvaRuntimeError("UDP media session is unavailable")
                    await udp_session.send_audio(
                        payload,
                        timestamp=self._downlink_timestamp,
                        generation=target.generation,
                    )
                else:
                    try:
                        media = self._binding.serialize_audio(
                            payload,
                            timestamp=self._downlink_timestamp,
                            target=target,
                        )
                    except RvaBindingError as exc:
                        if exc.code == "stale_generation":
                            interrupted = True
                            break
                        raise
                    await self._websocket.send_bytes(media)
                self._downlink_timestamp = (self._downlink_timestamp + SAMPLES_PER_PACKET) & 0xFFFFFFFF
                played_samples += min(3, len(frames)) * PCM_SAMPLES
            await self._send_pending_assistant_text(response_id, target)
            if not interrupted and target == self._binding.active_playback:
                end = await self._binding.response_end(response_id=response_id, target=target)
                await self._send_control_serialized(end)
        finally:
            if self._active_producer_epoch == segment.producer_epoch:
                self._active_producer_epoch = None
            await runner.playback_finished(played_samples / SAMPLE_RATE, interrupted)

    async def _send_pending_assistant_text(self, response_id: str, target: PlaybackRef) -> None:
        current = self._pending_assistant_text
        if not current or target != self._binding.active_playback:
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
            response_id=response_id,
            target=target,
            sequence=self._response_text_sequence,
            text=delta,
        )
        self._response_text_sequence += 1
        await self._send_control_serialized(event)

    async def _wait_playback_deadline(self, deadline: float, target: PlaybackRef) -> bool:
        remaining = deadline - self._clock()
        if remaining <= 0:
            return target == self._binding.active_playback
        self._generation_changed.clear()
        try:
            await asyncio.wait_for(self._generation_changed.wait(), timeout=remaining)
        except TimeoutError:
            return target == self._binding.active_playback
        return False

    async def _emit_segment(self, segment: AgentOutputSegment) -> None:
        if self._closed or segment.producer_epoch < self._minimum_producer_epoch or not segment.frames:
            return
        if len(segment.frames) > self._limits.max_segment_frames:
            error = RvaOverloadedError("agent output segment exceeds frame limit")
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

    def _emit_user_transcript(self, text: str, is_final: bool) -> None:
        if self._closed:
            return
        try:
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

    def _emit_assistant_text(self, text: str) -> None:
        if not self._closed and text:
            self._pending_assistant_text = text

    def _request_stop(self, producer_epoch: int) -> None:
        if self._closed:
            return
        self._minimum_producer_epoch = max(self._minimum_producer_epoch, producer_epoch)
        self._generation_changed.set()
        task = asyncio.create_task(self._finish_active_from_runner(), name=f"rva-stop-{self._binding_id}")
        self._aux_tasks.add(task)
        task.add_done_callback(self._on_aux_done)

    async def _finish_active_from_runner(self) -> None:
        target = self._binding.active_playback
        if target is None:
            return
        response_id = f"resp-{self._response_sequence:08d}"
        try:
            event = await self._binding.response_end(response_id=response_id, target=target, failed=True)
        except RvaBindingError as exc:
            if exc.code != "stale_generation":
                raise
            return
        await self._enqueue(_Outbound("control", event))

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
            raise RvaOverloadedError("output queue is full") from exc

    async def _send_control_serialized(self, payload: str) -> None:
        ack = asyncio.get_running_loop().create_future()
        await self._enqueue(_Outbound("control", payload, ack))
        await asyncio.wait_for(ack, timeout=self._limits.queue_timeout_seconds)

    async def _enqueue_segment(self, item: _Outbound) -> None:
        try:
            await asyncio.wait_for(self._segments.put(item), timeout=self._limits.queue_timeout_seconds)
        except TimeoutError as exc:
            raise RvaOverloadedError("segment queue is full") from exc

    def _enqueue_nowait(self, item: _Outbound) -> None:
        try:
            self._output.put_nowait(item)
        except asyncio.QueueFull as exc:
            raise RvaOverloadedError("output queue is full") from exc

    def _report_failure(self, error: BaseException) -> None:
        if self._failures.empty():
            self._failures.put_nowait(error)

    async def _receive_udp_audio(self, payload: bytes, timestamp: int, generation: int) -> None:
        self._mark_activity()
        if generation != self._binding.uplink_generation:
            return
        packet = InboundAudioPacket(self._udp_uplink_sequence, timestamp, payload)
        self._udp_uplink_sequence += 1
        await self._audio_port.receive_audio(packet)

    def _rva_udp_grant(self) -> dict[str, object] | None:
        session = self._udp_session
        if session is None:
            return None
        grant = session.grant
        return {
            "host": grant.host,
            "port": grant.port,
            "expires_at_ms": grant.expires_at * 1_000,
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
        try:
            await asyncio.wait_for(runner.close(), timeout=self._limits.runner_timeout_seconds)
        except TimeoutError as exc:
            raise RvaRuntimeError("agent runner close timed out") from exc

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
