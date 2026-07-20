"""Xiaozhi WebSocket v1 transport mapped onto the roomless AgentRunner."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import struct
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from fractions import Fraction
from typing import Literal

from av import AudioFrame, AudioResampler, CodecContext, Packet
from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect
from voice_contracts import LeaseRenewal

from ..agent import AgentOutputSegment, AgentRunner, create_runner
from ..audio import PCM_SAMPLES, PcmFrame
from ..auth import AuthContext
from ..config import Settings
from .xiaozhi_udp import UdpMediaGateway, UdpMediaSession

logger = logging.getLogger("realtime_worker.bindings.xiaozhi")

XIAOZHI_INPUT_SAMPLE_RATE = 16_000
XIAOZHI_OUTPUT_SAMPLE_RATE = 24_000
XIAOZHI_CHANNELS = 1
XIAOZHI_FRAME_DURATION_MS = 60
XIAOZHI_INPUT_PCM_SAMPLES = XIAOZHI_INPUT_SAMPLE_RATE * XIAOZHI_FRAME_DURATION_MS // 1000
XIAOZHI_PLAYBACK_PREBUFFER_PACKETS = 4
XIAOZHI_OPUS_APPLICATION = "audio"
XIAOZHI_WSS_PROFILE = "wss-opus-v1"
XIAOZHI_UDP_PROFILE = "udp-opus-gcm-v1"
_INPUT_TIME_BASE = Fraction(1, XIAOZHI_INPUT_SAMPLE_RATE)
_RUNNER_START_CANCEL_JOIN_SECONDS = 0.2
_HANDSHAKE_CANCEL_JOIN_SECONDS = 0.6


class XiaozhiProtocolError(ValueError):
    """A client message does not satisfy the fixed Xiaozhi v1 contract."""


class XiaozhiOverloadedError(RuntimeError):
    """A bounded media queue could not accept work in time."""


class XiaozhiMessageTooLarge(XiaozhiProtocolError):
    """A control or media frame exceeds the configured wire limit."""


def _consume_detached_task(task: asyncio.Task[None]) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("Detached Xiaozhi task failed task=%s", task.get_name())


@dataclass(frozen=True, slots=True)
class XiaozhiHello:
    version: int
    sample_rate: int
    channels: int
    frame_duration_ms: int
    transport_profiles: tuple[str, ...]
    transport_mode: Literal["auto", "force_wss", "force_udp_for_test"]


@dataclass(frozen=True, slots=True)
class XiaozhiClientMessage:
    kind: Literal["listen_start", "listen_stop", "listen_detect", "abort", "mcp"]
    payload: dict[str, object]


@dataclass(order=True, slots=True)
class _Outbound:
    priority: int
    sequence: int
    kind: str
    payload: bytes | dict[str, object]
    generation: int
    samples: int = 0
    interrupted: bool = False


class _AudioDiagnostics:
    """Keep bounded numeric uplink summaries without retaining audio."""

    def __init__(self, session_id: str, interval_packets: int) -> None:
        self._session_id = session_id
        self._interval_packets = interval_packets
        self._total_packets = 0
        self._window_packets = 0
        self._sample_count = 0
        self._nonzero_samples = 0
        self._peak = 0
        self._sum_squares = 0

    def observe(self, frames: list[PcmFrame]) -> None:
        self._total_packets += 1
        self._window_packets += 1
        for frame in frames:
            for (sample,) in struct.iter_unpack("<h", frame.pcm):
                magnitude = abs(sample)
                self._sample_count += 1
                self._nonzero_samples += sample != 0
                self._peak = max(self._peak, magnitude)
                self._sum_squares += sample * sample

        if self._total_packets == 1:
            self._log("first_packet")
            if self._interval_packets == 1:
                self._reset_window()
        elif self._total_packets % self._interval_packets == 0:
            self._log("summary")
            self._reset_window()

    def _log(self, event: str) -> None:
        rms = math.sqrt(self._sum_squares / self._sample_count) if self._sample_count else 0.0
        logger.info(
            "Xiaozhi audio diagnostics session=%s event=%s opus_packets=%d "
            "window_opus_packets=%d decoded_pcm_samples=%d pcm_peak=%d pcm_rms=%.2f pcm_nonzero_samples=%d",
            self._session_id,
            event,
            self._total_packets,
            self._window_packets,
            self._sample_count,
            self._peak,
            rms,
            self._nonzero_samples,
        )

    def _reset_window(self) -> None:
        self._window_packets = 0
        self._sample_count = 0
        self._nonzero_samples = 0
        self._peak = 0
        self._sum_squares = 0


def normalize_device_id(value: str | None) -> str | None:
    """Validate a device principal without changing its grant-bound identity."""

    if value is None or not value or len(value) > 64:
        return None
    if not all(char.isalnum() or char in "_.:-" for char in value):
        return None
    return value


def resolve_xiaozhi_device_id(device_id: str | None, client_id: str | None) -> str | None:
    """Use the physical Device-Id as principal; Client-Id never overrides it."""

    physical = normalize_device_id(device_id)
    if physical is None:
        return None
    if client_id is not None and normalize_device_id(client_id) is None:
        return None
    return physical


class SharedSessionAdmission:
    """One process-wide hard bound shared by every media route."""

    def __init__(self, max_sessions: int) -> None:
        self._max_sessions = max_sessions
        self._reservations: dict[str, tuple[str, str]] = {}
        self._principals: set[tuple[str, str]] = set()
        self._lock = asyncio.Lock()
        self._draining = False

    async def reserve(self, principal: tuple[str, str]) -> str | None:
        async with self._lock:
            if self._draining or len(self._reservations) >= self._max_sessions or principal in self._principals:
                return None
            token = uuid.uuid4().hex
            self._reservations[token] = principal
            self._principals.add(principal)
            return token

    async def release(self, token: str) -> None:
        async with self._lock:
            principal = self._reservations.pop(token, None)
            if principal is not None:
                self._principals.discard(principal)

    @property
    def active_count(self) -> int:
        return len(self._reservations)

    @property
    def draining(self) -> bool:
        return self._draining

    def set_draining(self, value: bool) -> None:
        self._draining = value


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise XiaozhiProtocolError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _parse_json(raw: str) -> dict[str, object]:
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except XiaozhiProtocolError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise XiaozhiProtocolError("malformed JSON") from exc
    if not isinstance(value, dict):
        raise XiaozhiProtocolError("message must be a JSON object")
    return value


def parse_client_hello(raw: str) -> XiaozhiHello:
    message = _parse_json(raw)
    if message.get("type") != "hello" or message.get("transport") != "websocket":
        raise XiaozhiProtocolError("expected WebSocket hello")
    if message.get("version") != 1:
        raise XiaozhiProtocolError("only Xiaozhi protocol version 1 is supported")
    audio = message.get("audio_params")
    if not isinstance(audio, dict):
        raise XiaozhiProtocolError("hello requires audio_params")
    expected = {
        "format": "opus",
        "sample_rate": XIAOZHI_INPUT_SAMPLE_RATE,
        "channels": XIAOZHI_CHANNELS,
        "frame_duration": XIAOZHI_FRAME_DURATION_MS,
    }
    if any(audio.get(key) != value for key, value in expected.items()):
        raise XiaozhiProtocolError("unsupported audio_params")
    raw_profiles = message.get("transport_profiles", [XIAOZHI_WSS_PROFILE])
    if (
        not isinstance(raw_profiles, list)
        or not 1 <= len(raw_profiles) <= 4
        or any(not isinstance(profile, str) for profile in raw_profiles)
        or len(set(raw_profiles)) != len(raw_profiles)
    ):
        raise XiaozhiProtocolError("transport_profiles must be a unique bounded string array")
    allowed_profiles = {XIAOZHI_WSS_PROFILE, XIAOZHI_UDP_PROFILE}
    if any(profile not in allowed_profiles for profile in raw_profiles):
        raise XiaozhiProtocolError("unsupported transport profile capability")
    transport_mode = message.get("transport_mode", "auto")
    if transport_mode not in {"auto", "force_wss", "force_udp_for_test"}:
        raise XiaozhiProtocolError("unsupported transport mode")
    return XiaozhiHello(
        version=1,
        sample_rate=XIAOZHI_INPUT_SAMPLE_RATE,
        channels=XIAOZHI_CHANNELS,
        frame_duration_ms=XIAOZHI_FRAME_DURATION_MS,
        transport_profiles=tuple(raw_profiles),
        transport_mode=transport_mode,
    )


def parse_client_message(raw: str, session_id: str) -> XiaozhiClientMessage:
    message = _parse_json(raw)
    if message.get("session_id") != session_id:
        raise XiaozhiProtocolError("session_id does not match the active session")
    message_type = message.get("type")
    if message_type == "abort":
        return XiaozhiClientMessage("abort", message)
    if message_type == "mcp":
        return XiaozhiClientMessage("mcp", message)
    if message_type != "listen":
        raise XiaozhiProtocolError("unsupported client message type")
    state = message.get("state")
    if state == "start":
        if message.get("mode") not in {"auto", "manual", "realtime"}:
            raise XiaozhiProtocolError("listen start requires a supported mode")
        return XiaozhiClientMessage("listen_start", message)
    if state == "stop":
        return XiaozhiClientMessage("listen_stop", message)
    if state == "detect" and isinstance(message.get("text"), str):
        return XiaozhiClientMessage("listen_detect", message)
    raise XiaozhiProtocolError("unsupported listen state")


class XiaozhiOpusCodec:
    """Stateful libopus codec for Xiaozhi's raw 60 ms packet boundary."""

    def __init__(self, *, encode_sample_rate: int = XIAOZHI_INPUT_SAMPLE_RATE) -> None:
        if encode_sample_rate not in {XIAOZHI_INPUT_SAMPLE_RATE, XIAOZHI_OUTPUT_SAMPLE_RATE}:
            raise ValueError("unsupported Xiaozhi Opus encoder sample rate")
        decoder = CodecContext.create("opus", "r")
        decoder.sample_rate = XIAOZHI_INPUT_SAMPLE_RATE
        decoder.open()
        self._decoder = decoder
        self._decoder_resampler = AudioResampler(format="s16", layout="mono", rate=XIAOZHI_INPUT_SAMPLE_RATE)

        encoder = CodecContext.create("libopus", "w")
        encoder.sample_rate = encode_sample_rate
        encoder.layout = "mono"
        encoder.format = "s16"
        encoder.bit_rate = 24_000
        encoder.time_base = Fraction(1, encode_sample_rate)
        # Match the proven Xiaozhi server profile. APPLICATION_AUDIO avoids
        # SILK/hybrid mode switching that can sound discontinuous on esp_opus_dec.
        encoder.options = {
            "application": XIAOZHI_OPUS_APPLICATION,
            "frame_duration": str(XIAOZHI_FRAME_DURATION_MS),
        }
        encoder.open()
        self._encoder = encoder
        self._encode_sample_rate = encode_sample_rate
        self._encoder_resampler = (
            AudioResampler(format="s16", layout="mono", rate=encode_sample_rate)
            if encode_sample_rate != XIAOZHI_INPUT_SAMPLE_RATE
            else None
        )
        self._encoder_pcm = bytearray()
        self._encoder_pts = 0

    def decode_60ms(self, payload: bytes, *, sequence_start: int) -> list[PcmFrame]:
        decoded = self._decoder.decode(Packet(payload))
        decoded_duration = sum(frame.samples / frame.sample_rate for frame in decoded)
        if abs(decoded_duration - XIAOZHI_FRAME_DURATION_MS / 1000) > 0.001:
            raise ValueError("Opus packet does not contain exactly 60 ms of audio")
        pcm = bytearray()
        for frame in decoded:
            resampled = self._decoder_resampler.resample(frame)
            frames = resampled if isinstance(resampled, list) else [resampled]
            for converted in frames:
                if converted is None:
                    continue
                pcm.extend(bytes(converted.planes[0])[: converted.samples * 2])
        expected_bytes = XIAOZHI_INPUT_PCM_SAMPLES * 2
        # FFmpeg's resampler retains a small startup tail. Preserve the fixed
        # transport cadence; subsequent packets recover the steady-state rate.
        if expected_bytes - 64 <= len(pcm) < expected_bytes:
            pcm.extend(b"\x00" * (expected_bytes - len(pcm)))
        if len(pcm) != expected_bytes:
            raise ValueError(f"Opus packet decoded to {len(pcm)} bytes, expected {expected_bytes}")
        result: list[PcmFrame] = []
        for index in range(XIAOZHI_INPUT_PCM_SAMPLES // PCM_SAMPLES):
            sequence = sequence_start + index
            offset = index * PCM_SAMPLES * 2
            result.append(
                PcmFrame(
                    1,
                    sequence,
                    sequence * PCM_SAMPLES,
                    bytes(pcm[offset : offset + PCM_SAMPLES * 2]),
                )
            )
        return result

    def encode_60ms(self, frames: list[PcmFrame]) -> bytes:
        if not 1 <= len(frames) <= 3:
            raise ValueError("a Xiaozhi packet requires one to three 20 ms PCM frames")
        pcm = b"".join(frame.pcm for frame in frames)
        pcm += b"\x00" * (XIAOZHI_INPUT_PCM_SAMPLES * 2 - len(pcm))
        if self._encoder_resampler is not None:
            source = AudioFrame(format="s16", layout="mono", samples=XIAOZHI_INPUT_PCM_SAMPLES)
            source.sample_rate = XIAOZHI_INPUT_SAMPLE_RATE
            source.time_base = _INPUT_TIME_BASE
            source.planes[0].update(pcm)
            converted = self._encoder_resampler.resample(source)
            converted_frames = converted if isinstance(converted, list) else [converted]
            for frame in converted_frames:
                if frame is not None:
                    self._encoder_pcm.extend(bytes(frame.planes[0])[: frame.samples * 2])
        else:
            self._encoder_pcm.extend(pcm)

        encoder_samples = self._encode_sample_rate * XIAOZHI_FRAME_DURATION_MS // 1000
        expected_bytes = encoder_samples * 2
        if self._encoder_pts == 0 and len(self._encoder_pcm) < expected_bytes:
            self._encoder_pcm[:0] = b"\x00" * (expected_bytes - len(self._encoder_pcm))
        if len(self._encoder_pcm) < expected_bytes:
            raise ValueError("resampler did not produce one 60 ms Xiaozhi output frame")
        packet_pcm = bytes(self._encoder_pcm[:expected_bytes])
        del self._encoder_pcm[:expected_bytes]

        audio = AudioFrame(format="s16", layout="mono", samples=encoder_samples)
        audio.sample_rate = self._encode_sample_rate
        audio.time_base = Fraction(1, self._encode_sample_rate)
        audio.pts = self._encoder_pts
        audio.planes[0].update(packet_pcm)
        self._encoder_pts += encoder_samples
        packets = self._encoder.encode(audio)
        if len(packets) != 1:
            raise ValueError(f"libopus emitted {len(packets)} packets for one 60 ms frame")
        return bytes(packets[0])


RunnerFactory = Callable[
    [Settings, Callable[[AgentOutputSegment], Awaitable[None]], Callable[[int], None]],
    AgentRunner,
]


class XiaozhiConnection:
    """One authenticated Xiaozhi socket with bounded codec and writer owners."""

    def __init__(
        self,
        websocket: WebSocket,
        auth: AuthContext,
        settings: Settings,
        *,
        runner_factory: RunnerFactory = create_runner,
        udp_gateway: UdpMediaGateway | None = None,
    ) -> None:
        self._websocket = websocket
        self._auth = auth
        self._settings = settings
        self._runner_factory = runner_factory
        self._udp_gateway = udp_gateway
        self._udp_session: UdpMediaSession | None = None
        self._transport_profile = XIAOZHI_WSS_PROFILE
        self.session_id = f"sess_{uuid.uuid4().hex}"
        self._codec: XiaozhiOpusCodec | None = None
        self._input: asyncio.Queue[bytes | None] = asyncio.Queue(settings.xiaozhi_media_queue_frames)
        self._output: asyncio.PriorityQueue[_Outbound] = asyncio.PriorityQueue(settings.xiaozhi_media_queue_frames)
        self._failures: asyncio.Queue[BaseException] = asyncio.Queue(1)
        self._runner: AgentRunner | None = None
        self._runner_lock = asyncio.Lock()
        self._minimum_output_epoch = 1
        self._output_fenced = False
        self._tasks: set[asyncio.Task[None]] = set()
        self._prefetched_frames: deque[dict[str, object]] = deque()
        self._stop_task: asyncio.Task[None] | None = None
        self._abort_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._assistant_text_pending: _Outbound | None = None
        self._closed = False
        self._listening = False
        self._input_sequence = 0
        self._output_sequence = 0
        self._generation = 1
        self._generation_changed = asyncio.Event()
        self._udp_handshake_generation = 0
        self._played_samples = 0
        self._playback_active = False
        self._audio_diagnostics = (
            _AudioDiagnostics(self.session_id, settings.xiaozhi_audio_diagnostics_interval_packets)
            if settings.xiaozhi_audio_diagnostics
            else None
        )

    @property
    def auth_context(self) -> AuthContext:
        return self._auth

    async def run(self) -> None:
        close_code = 1000
        close_reason = "normal"
        try:
            hello_frame = await asyncio.wait_for(
                self._websocket.receive(), timeout=self._settings.xiaozhi_handshake_timeout_seconds
            )
            hello_raw = hello_frame.get("text")
            if not isinstance(hello_raw, str):
                raise XiaozhiProtocolError("first frame must be a text hello")
            if len(hello_raw.encode("utf-8")) > self._settings.max_control_bytes:
                raise XiaozhiMessageTooLarge("hello is too large")
            hello = parse_client_hello(hello_raw)
            self._transport_profile = self._select_transport(hello)
            self._codec = await asyncio.to_thread(XiaozhiOpusCodec, encode_sample_rate=XIAOZHI_OUTPUT_SAMPLE_RATE)
            if self._transport_profile == XIAOZHI_UDP_PROFILE:
                assert self._udp_gateway is not None
                self._udp_session = self._udp_gateway.create_session(self._accept_udp_audio, self._report_failure)
                await self._websocket.send_json(self._server_hello())
                await self._complete_udp_handshake()
            else:
                async with self._runner_lock:
                    await self._start_runner_locked()
                await self._websocket.send_json(self._server_hello())
            self._tasks = {
                asyncio.create_task(self._reader_loop(), name=f"xiaozhi-reader-{self.session_id}"),
                asyncio.create_task(self._input_loop(), name=f"xiaozhi-input-{self.session_id}"),
                asyncio.create_task(self._writer_loop(), name=f"xiaozhi-writer-{self.session_id}"),
                asyncio.create_task(self._failure_loop(), name=f"xiaozhi-failure-{self.session_id}"),
            }
            done, _ = await asyncio.wait(self._tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task.cancelled():
                    if self._closed:
                        return
                    raise RuntimeError(f"Xiaozhi child task was cancelled unexpectedly: {task.get_name()}")
                exception = task.exception()
                if exception is not None:
                    raise exception
        except WebSocketDisconnect:
            return
        except TimeoutError:
            close_code, close_reason = 1008, "handshake_timeout"
        except XiaozhiMessageTooLarge:
            close_code, close_reason = 1009, "message_too_large"
        except XiaozhiProtocolError:
            close_code, close_reason = 1002, "protocol_error"
        except XiaozhiOverloadedError:
            close_code, close_reason = 1013, "media_overloaded"
        except Exception:
            logger.exception("Xiaozhi session failed device=%s", self._auth.device_id)
            close_code, close_reason = 1011, "runtime_failure"
        finally:
            await self.close(code=close_code, reason=close_reason)

    async def close(self, *, code: int = 1000, reason: str = "normal") -> None:
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(
                self._close_impl(code=code, reason=reason),
                name=f"xiaozhi-close-{self.session_id}",
            )
        await asyncio.shield(self._close_task)

    async def _close_impl(self, *, code: int, reason: str) -> None:
        current = asyncio.current_task()
        for task in self._tasks:
            if task is not current:
                task.cancel()
        await asyncio.gather(*(task for task in self._tasks if task is not current), return_exceptions=True)
        for task in (self._stop_task, self._abort_task):
            if task is not None and task is not current:
                task.cancel()
        await asyncio.gather(
            *(task for task in (self._stop_task, self._abort_task) if task is not None and task is not current),
            return_exceptions=True,
        )
        self._tasks.clear()
        self._stop_task = None
        self._abort_task = None
        runner, self._runner = self._runner, None
        if runner is not None:
            try:
                await asyncio.wait_for(runner.close(), timeout=5.0)
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is None or task.cancelling():
                    raise
                logger.warning("Xiaozhi runner cancelled its own cleanup device=%s", self._auth.device_id)
            except Exception:
                logger.warning("Xiaozhi runner cleanup failed device=%s", self._auth.device_id)
        udp_session, self._udp_session = self._udp_session, None
        if udp_session is not None:
            await udp_session.close()
        try:
            await self._websocket.close(code=code, reason=reason)
        except (RuntimeError, WebSocketDisconnect):
            pass

    async def _reader_loop(self) -> None:
        while True:
            message = await self._receive_frame()
            message_type = message.get("type")
            if message_type == "websocket.disconnect":
                return
            text = message.get("text")
            payload = message.get("bytes")
            if isinstance(text, str):
                if len(text.encode("utf-8")) > self._settings.max_control_bytes:
                    raise XiaozhiMessageTooLarge("control message is too large")
                await self._handle_control(parse_client_message(text, self.session_id))
            elif isinstance(payload, bytes):
                if self._transport_profile != XIAOZHI_WSS_PROFILE:
                    raise XiaozhiProtocolError("binary WSS media is disabled for the selected profile")
                if len(payload) > self._settings.xiaozhi_max_opus_bytes:
                    raise XiaozhiMessageTooLarge("Opus packet is too large")
                if not payload:
                    raise XiaozhiProtocolError("Opus packet is empty")
                if not self._listening:
                    continue
                try:
                    self._input.put_nowait(payload)
                except asyncio.QueueFull as exc:
                    raise XiaozhiOverloadedError("input media queue is full") from exc
            else:
                raise XiaozhiProtocolError("unsupported WebSocket frame")

    async def _receive_frame(self) -> dict[str, object]:
        if self._prefetched_frames:
            return self._prefetched_frames.popleft()
        return await self._websocket.receive()

    async def _complete_udp_handshake(self) -> None:
        udp_session = self._udp_session
        if udp_session is None:
            raise RuntimeError("UDP session is unavailable during handshake")
        probe_task = asyncio.create_task(
            udp_session.wait_ready(self._settings.xiaozhi_udp_probe_timeout_seconds),
            name=f"xiaozhi-udp-probe-{self.session_id}",
        )
        reader_task = asyncio.create_task(
            self._udp_handshake_reader(), name=f"xiaozhi-handshake-reader-{self.session_id}"
        )
        setup_task: asyncio.Task[None] | None = None
        self._udp_handshake_generation += 1
        handshake_generation = self._udp_handshake_generation
        try:
            await self._race_udp_handshake_step(
                probe_task,
                reader_task,
                timeout=self._settings.xiaozhi_udp_probe_timeout_seconds,
            )
            setup_task = asyncio.create_task(
                self._start_udp_runner_and_mark_ready(handshake_generation),
                name=f"xiaozhi-udp-setup-{self.session_id}",
            )
            await self._race_udp_handshake_step(
                setup_task,
                reader_task,
                timeout=self._settings.xiaozhi_handshake_timeout_seconds,
            )
        finally:
            if self._udp_handshake_generation == handshake_generation:
                self._udp_handshake_generation += 1
            tasks = (probe_task, reader_task, setup_task)
            await self._cancel_tasks_bounded(
                (task for task in tasks if task is not None),
                timeout=_HANDSHAKE_CANCEL_JOIN_SECONDS,
                context="UDP handshake",
            )

    async def _udp_handshake_reader(self) -> None:
        while True:
            frame = await self._websocket.receive()
            if frame.get("type") == "websocket.disconnect":
                raise WebSocketDisconnect(code=int(frame.get("code", 1000)))
            if len(self._prefetched_frames) >= self._settings.xiaozhi_media_queue_frames:
                raise XiaozhiOverloadedError("pre-ready control queue is full")
            self._prefetched_frames.append(frame)

    async def _race_udp_handshake_step(
        self,
        operation: asyncio.Task[None],
        reader: asyncio.Task[None],
        *,
        timeout: float,
    ) -> None:
        async with asyncio.timeout(timeout):
            done, _ = await asyncio.wait({operation, reader}, return_when=asyncio.FIRST_COMPLETED)
            if reader in done:
                await reader
            await operation

    async def _start_udp_runner_and_mark_ready(self, handshake_generation: int) -> None:
        async with self._runner_lock:
            await self._start_runner_locked()
        if self._closed or self._udp_handshake_generation != handshake_generation:
            return
        await self._websocket.send_json(
            {
                "session_id": self.session_id,
                "type": "media",
                "state": "ready",
                "transport_profile": self._transport_profile,
            }
        )

    async def _handle_control(self, message: XiaozhiClientMessage) -> None:
        if message.kind == "listen_start":
            self._listening = True
        elif message.kind == "listen_stop":
            self._listening = False
        elif message.kind == "abort":
            await self._abort_current_response()
        elif message.kind in {"listen_detect", "mcp"}:
            return

    async def _accept_udp_audio(self, payload: bytes, timestamp: int, generation: int) -> None:
        del timestamp
        if self._closed or not self._listening or generation != self._generation:
            return
        if len(payload) > self._settings.xiaozhi_max_opus_bytes:
            raise XiaozhiMessageTooLarge("UDP Opus packet is too large")
        try:
            self._input.put_nowait(payload)
        except asyncio.QueueFull as exc:
            raise XiaozhiOverloadedError("input media queue is full") from exc

    async def _input_loop(self) -> None:
        while True:
            payload = await self._input.get()
            if payload is None:
                return
            codec = self._codec
            if codec is None:
                raise XiaozhiProtocolError("codec is not ready")
            try:
                frames = await asyncio.to_thread(codec.decode_60ms, payload, sequence_start=self._input_sequence)
            except Exception as exc:
                raise XiaozhiProtocolError("invalid Opus packet") from exc
            if self._audio_diagnostics is not None:
                self._audio_diagnostics.observe(frames)
            self._input_sequence += len(frames)
            async with self._runner_lock:
                runner = self._runner
                if runner is None:
                    continue
                for frame in frames:
                    try:
                        await runner.push_audio(frame)
                    except BufferError as exc:
                        raise XiaozhiOverloadedError("Agent input is full") from exc

    async def _emit_segment(self, segment: AgentOutputSegment) -> None:
        try:
            if (
                self._closed
                or not segment.frames
                or self._output_fenced
                or segment.producer_epoch < self._minimum_output_epoch
            ):
                return
            codec = self._codec
            if codec is None:
                raise XiaozhiProtocolError("codec is not ready")
            generation = self._generation
            producer_epoch = segment.producer_epoch
            await self._enqueue("text", self._tts_event("start"), generation)
            for offset in range(0, len(segment.frames), 3):
                if self._output_fenced or producer_epoch < self._minimum_output_epoch or generation != self._generation:
                    return
                packet = await asyncio.to_thread(codec.encode_60ms, segment.frames[offset : offset + 3])
                await self._enqueue(
                    "audio",
                    packet,
                    generation,
                    samples=min(3, len(segment.frames) - offset) * PCM_SAMPLES,
                )
            if self._output_fenced or producer_epoch < self._minimum_output_epoch or generation != self._generation:
                return
            await self._enqueue("text", self._tts_event("stop"), generation)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if self._failures.empty():
                self._failures.put_nowait(exc)
            raise

    async def _enqueue(
        self,
        kind: str,
        payload: bytes | dict[str, object],
        generation: int,
        *,
        samples: int = 0,
        priority: int = 10,
        interrupted: bool = False,
    ) -> None:
        self._output_sequence += 1
        item = _Outbound(priority, self._output_sequence, kind, payload, generation, samples, interrupted)
        try:
            await asyncio.wait_for(self._output.put(item), timeout=self._settings.xiaozhi_queue_timeout_seconds)
        except TimeoutError as exc:
            raise XiaozhiOverloadedError("output media queue is full") from exc

    def _enqueue_control_nowait(self, payload: dict[str, object], *, priority: int = 5) -> _Outbound | None:
        if self._closed:
            return None
        if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > self._settings.max_control_bytes:
            self._report_failure(XiaozhiMessageTooLarge("outbound text event is too large"))
            return None
        self._output_sequence += 1
        item = _Outbound(priority, self._output_sequence, "text", payload, self._generation)
        try:
            self._output.put_nowait(item)
        except asyncio.QueueFull:
            self._report_failure(XiaozhiOverloadedError("output media queue is full"))
            return None
        return item

    def _emit_user_transcript(self, text: str, is_final: bool) -> None:
        self._enqueue_control_nowait(
            {
                "session_id": self.session_id,
                "type": "stt",
                "text": text,
                "is_final": is_final,
            }
        )

    def _emit_assistant_text(self, text: str) -> None:
        if self._output_fenced:
            return
        payload: dict[str, object] = {
            "session_id": self.session_id,
            "type": "tts",
            "state": "sentence_start",
            "text": text,
        }
        if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > self._settings.max_control_bytes:
            self._report_failure(XiaozhiMessageTooLarge("outbound text event is too large"))
            return
        pending = self._assistant_text_pending
        if pending is not None and pending.generation == self._generation:
            pending.payload = payload
            return
        self._assistant_text_pending = self._enqueue_control_nowait(payload)

    def _report_failure(self, error: BaseException) -> None:
        if self._failures.empty():
            self._failures.put_nowait(error)

    async def _writer_loop(self) -> None:
        playback_started_at: float | None = None
        playback_packets = 0
        while True:
            item = await self._output.get()
            if item is self._assistant_text_pending:
                self._assistant_text_pending = None
            if item.generation != self._generation:
                continue
            if item.kind == "audio":
                assert isinstance(item.payload, bytes)
                if playback_started_at is not None and playback_packets >= XIAOZHI_PLAYBACK_PREBUFFER_PACKETS:
                    deadline = playback_started_at + (playback_packets - XIAOZHI_PLAYBACK_PREBUFFER_PACKETS + 1) * (
                        XIAOZHI_FRAME_DURATION_MS / 1000
                    )
                    if not await self._wait_for_playback_deadline(deadline, item.generation):
                        continue
                if self._transport_profile == XIAOZHI_UDP_PROFILE:
                    udp_session = self._udp_session
                    if udp_session is None:
                        raise RuntimeError("selected UDP media session is unavailable")
                    await udp_session.send_audio(
                        item.payload,
                        timestamp=self._played_samples,
                        generation=item.generation,
                    )
                else:
                    await self._websocket.send_bytes(item.payload)
                self._played_samples += item.samples
                playback_packets += 1
                continue
            assert isinstance(item.payload, dict)
            state = item.payload.get("state")
            if state == "stop" and not item.interrupted and playback_started_at is not None and playback_packets > 0:
                playback_end = playback_started_at + playback_packets * (XIAOZHI_FRAME_DURATION_MS / 1000)
                if not await self._wait_for_playback_deadline(playback_end, item.generation):
                    continue
            await self._websocket.send_json(item.payload)
            runner = self._runner
            if state == "start" and runner is not None:
                self._generation_changed.clear()
                playback_started_at = time.monotonic()
                playback_packets = 0
                self._played_samples = 0
                self._playback_active = True
                await runner.playback_started(time.time())
            elif state == "start":
                self._generation_changed.clear()
                playback_started_at = time.monotonic()
                playback_packets = 0
                self._played_samples = 0
            elif state == "stop" and runner is not None and self._playback_active:
                self._playback_active = False
                await runner.playback_finished(
                    self._played_samples / XIAOZHI_INPUT_SAMPLE_RATE,
                    item.interrupted,
                )
                playback_started_at = None
                playback_packets = 0

    async def _wait_for_playback_deadline(self, deadline: float, generation: int) -> bool:
        delay = deadline - time.monotonic()
        if delay > 0:
            try:
                await asyncio.wait_for(self._generation_changed.wait(), timeout=delay)
            except TimeoutError:
                pass
        return generation == self._generation

    async def _failure_loop(self) -> None:
        raise await self._failures.get()

    def _request_stop(self, producer_epoch: int) -> None:
        if self._closed:
            return
        self._minimum_output_epoch = max(self._minimum_output_epoch, producer_epoch)
        if self._stop_task is not None and not self._stop_task.done():
            return
        self._stop_task = asyncio.create_task(self._fence_playback(), name=f"xiaozhi-stop-{self.session_id}")
        self._stop_task.add_done_callback(self._on_stop_done)

    def _on_stop_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None and self._failures.empty():
            self._failures.put_nowait(exception)

    async def _fence_playback(self) -> None:
        self._generation += 1
        self._generation_changed.set()
        await self._enqueue(
            "text",
            self._tts_event("stop"),
            self._generation,
            priority=0,
            interrupted=True,
        )

    async def _abort_current_response(self) -> None:
        if self._abort_task is None or self._abort_task.done():
            self._abort_task = asyncio.create_task(
                self._interrupt_runner(), name=f"xiaozhi-interrupt-{self.session_id}"
            )
        await self._abort_task

    async def _interrupt_runner(self) -> None:
        self._output_fenced = True
        await self._fence_playback()
        async with self._runner_lock:
            runner = self._runner
            if runner is not None:
                producer_epoch = await asyncio.wait_for(runner.interrupt(), timeout=5.0)
                self._minimum_output_epoch = max(self._minimum_output_epoch, producer_epoch)
        self._output_fenced = False

    async def _start_runner_locked(self) -> None:
        runner = self._runner_factory(self._settings, self._emit_segment, self._request_stop)
        self._runner = runner
        configure_text_sinks = getattr(runner, "set_text_sinks", None)
        if callable(configure_text_sinks):
            configure_text_sinks(self._emit_user_transcript, self._emit_assistant_text)
        start_task = asyncio.create_task(
            runner.start(),
            name=f"xiaozhi-runner-start-{self.session_id}",
        )
        try:
            done, _ = await asyncio.wait(
                {start_task},
                timeout=self._settings.xiaozhi_handshake_timeout_seconds,
            )
            if start_task not in done:
                raise TimeoutError
            await start_task
        except BaseException:
            await self._cancel_tasks_bounded(
                (start_task,),
                timeout=_RUNNER_START_CANCEL_JOIN_SECONDS,
                context="runner start",
            )
            raise

    async def _cancel_tasks_bounded(
        self,
        tasks: Iterable[asyncio.Task[None]],
        *,
        timeout: float,
        context: str,
    ) -> None:
        owned = set(tasks)
        if not owned:
            return
        for task in owned:
            if not task.done():
                task.cancel()
        done, pending = await asyncio.wait(owned, timeout=timeout / 2)
        for task in pending:
            task.cancel()
        if pending:
            retried_done, pending = await asyncio.wait(pending, timeout=timeout / 2)
            done.update(retried_done)
        for task in done:
            if not task.cancelled():
                task.exception()
        if pending:
            names = ",".join(sorted(task.get_name() for task in pending))
            logger.critical(
                "Xiaozhi detaching non-cooperative task device=%s context=%s tasks=%s",
                self._auth.device_id,
                context,
                names,
            )
            for task in pending:
                task.add_done_callback(_consume_detached_task)

    def _tts_event(self, state: str) -> dict[str, object]:
        payload: dict[str, object] = {
            "session_id": self.session_id,
            "type": "tts",
            "state": state,
        }
        if self._transport_profile == XIAOZHI_UDP_PROFILE:
            payload["generation"] = self._generation
        return payload

    def _select_transport(self, hello: XiaozhiHello) -> str:
        policy = self._settings.xiaozhi_transport_policy
        allowed = set(self._auth.allowed_profiles)
        supports_wss = XIAOZHI_WSS_PROFILE in hello.transport_profiles and XIAOZHI_WSS_PROFILE in allowed
        supports_udp = XIAOZHI_UDP_PROFILE in hello.transport_profiles and XIAOZHI_UDP_PROFILE in allowed
        udp_available = self._udp_gateway is not None and self._udp_gateway.is_ready
        if policy == "force_wss":
            if hello.transport_mode == "force_udp_for_test" or not supports_wss:
                raise XiaozhiProtocolError("forced WSS profile conflicts with client capabilities")
            return XIAOZHI_WSS_PROFILE
        if policy == "force_udp_for_test":
            if hello.transport_mode == "force_wss":
                raise XiaozhiProtocolError("forced UDP profile conflicts with client policy")
            if not supports_udp or not udp_available:
                raise XiaozhiProtocolError("forced UDP profile is unavailable")
            return XIAOZHI_UDP_PROFILE
        if hello.transport_mode == "force_wss":
            if not supports_wss:
                raise XiaozhiProtocolError("client-selected WSS profile is unavailable")
            return XIAOZHI_WSS_PROFILE
        if hello.transport_mode == "force_udp_for_test":
            if not supports_udp or not udp_available:
                raise XiaozhiProtocolError("client-selected UDP profile is unavailable")
            return XIAOZHI_UDP_PROFILE
        # auto/prefer_device remains conservative until the UDP promotion gate passes.
        if supports_wss:
            return XIAOZHI_WSS_PROFILE
        if supports_udp and udp_available:
            return XIAOZHI_UDP_PROFILE
        raise XiaozhiProtocolError("no mutually supported transport profile")

    def _server_hello(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "type": "hello",
            "version": 1,
            "transport": "udp" if self._transport_profile == XIAOZHI_UDP_PROFILE else "websocket",
            "transport_profile": self._transport_profile,
            "session_id": self.session_id,
            "audio_params": {
                "format": "opus",
                "sample_rate": XIAOZHI_OUTPUT_SAMPLE_RATE,
                "channels": XIAOZHI_CHANNELS,
                "frame_duration": XIAOZHI_FRAME_DURATION_MS,
            },
        }
        if self._transport_profile == XIAOZHI_UDP_PROFILE:
            assert self._udp_session is not None
            payload["udp"] = self._udp_session.grant.as_control_payload()
        return payload


class XiaozhiSessionRegistry:
    """Own Xiaozhi connections and enforce bounded per-principal allocation."""

    def __init__(
        self,
        settings: Settings,
        admission: SharedSessionAdmission,
        *,
        runner_factory: RunnerFactory = create_runner,
        udp_gateway: UdpMediaGateway | None = None,
    ) -> None:
        self._settings = settings
        self._admission = admission
        self._runner_factory = runner_factory
        self._udp_gateway = udp_gateway
        self._connections: dict[tuple[str, str], XiaozhiConnection] = {}
        self._lease_deadlines: dict[str, float] = {}
        self._pending_releases: deque[LeaseRenewal] = deque(maxlen=64)
        self._lock = asyncio.Lock()

    async def run(self, websocket: WebSocket, auth: AuthContext) -> None:
        principal = (auth.tenant_id, auth.device_id)
        token = await self._admission.reserve(principal)
        if token is None:
            await websocket.close(code=1013, reason="session_overloaded")
            return
        async with self._lock:
            connection = XiaozhiConnection(
                websocket,
                auth,
                self._settings,
                runner_factory=self._runner_factory,
                udp_gateway=self._udp_gateway,
            )
            self._connections[principal] = connection
            if auth.session_epoch is not None and auth.expires_at is not None:
                self._lease_deadlines[auth.session_epoch] = auth.expires_at
        try:
            await connection.run()
        finally:
            async with self._lock:
                if self._connections.get(principal) is connection:
                    self._connections.pop(principal, None)
                if connection.auth_context.session_epoch is not None:
                    self._lease_deadlines.pop(connection.auth_context.session_epoch, None)
                release = self._lease_claim(connection.auth_context)
                if release is not None:
                    self._pending_releases.append(release)
            await self._admission.release(token)

    async def close(self) -> None:
        async with self._lock:
            connections = tuple(self._connections.values())
            self._connections.clear()
        await asyncio.gather(
            *(connection.close(code=1001, reason="server_shutdown") for connection in connections),
            return_exceptions=True,
        )

    async def revoke_session_epochs(self, session_epochs: set[str]) -> None:
        if not session_epochs:
            return
        async with self._lock:
            connections = tuple(
                connection
                for connection in self._connections.values()
                if connection.auth_context.session_epoch in session_epochs
            )
        await asyncio.gather(
            *(connection.close(code=1008, reason="stale_route_lease") for connection in connections),
            return_exceptions=True,
        )

    async def revoke_expired_leases(self, now: float) -> None:
        expired = {epoch for epoch, deadline in self._lease_deadlines.items() if deadline <= now}
        await self.revoke_session_epochs(expired)

    def extend_lease_deadlines(self, expires_at: float, rejected_epochs: set[str]) -> None:
        for session_epoch in tuple(self._lease_deadlines):
            if session_epoch not in rejected_epochs:
                self._lease_deadlines[session_epoch] = expires_at

    def active_lease_renewals(self) -> tuple[LeaseRenewal, ...]:
        renewals: list[LeaseRenewal] = []
        for connection in self._connections.values():
            auth = connection.auth_context
            if auth.session_epoch is not None and auth.fencing_token is not None:
                renewals.append(
                    LeaseRenewal(
                        tenant_id=auth.tenant_id,
                        device_id=auth.device_id,
                        session_epoch=auth.session_epoch,
                        fencing_token=auth.fencing_token,
                    )
                )
        return tuple(renewals)

    def pending_lease_releases(self) -> tuple[LeaseRenewal, ...]:
        return tuple(self._pending_releases)

    def acknowledge_lease_releases(self, releases: tuple[LeaseRenewal, ...]) -> None:
        acknowledged = {
            (release.tenant_id, release.device_id, release.session_epoch, release.fencing_token) for release in releases
        }
        self._pending_releases = deque(
            (
                release
                for release in self._pending_releases
                if (release.tenant_id, release.device_id, release.session_epoch, release.fencing_token)
                not in acknowledged
            ),
            maxlen=64,
        )

    @staticmethod
    def _lease_claim(auth: AuthContext) -> LeaseRenewal | None:
        if auth.session_epoch is None or auth.fencing_token is None:
            return None
        return LeaseRenewal(
            tenant_id=auth.tenant_id,
            device_id=auth.device_id,
            session_epoch=auth.session_epoch,
            fencing_token=auth.fencing_token,
        )

    @property
    def active_count(self) -> int:
        return len(self._connections)
