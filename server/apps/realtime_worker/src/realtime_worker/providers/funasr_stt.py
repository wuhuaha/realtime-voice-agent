"""FunASR 2pass WebSocket transport and its LiveKit STT adapter.

The transport is deliberately testable without LiveKit.  The small LiveKit
wrapper only converts audio frames and transcript events at the public plugin
boundary.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from livekit import rtc
from livekit.agents import DEFAULT_API_CONNECT_OPTIONS, APIConnectionError, stt, utils

from ..config import Settings
from ..errors import BackpressureError, ProviderError
from ..observability.asr_recording import AsrWavRecorder
from ..observability.events import Tracer, redact_exception


class RecognitionKind(StrEnum):
    INTERIM = "interim"
    FINAL = "final"


class FunASRProtocol(StrEnum):
    LOCAL = "local"
    STANDALONE = "standalone"


def _bundled_hotwords() -> tuple[str, ...]:
    path = Path(__file__).resolve().parents[1] / "resources" / "funasr_hotwords.txt"
    return tuple(line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


@dataclass(frozen=True)
class RecognitionEvent:
    kind: RecognitionKind
    text: str
    segment_id: str
    request_id: str
    raw_mode: str


@dataclass(frozen=True)
class FunASRStreamConfig:
    url: str
    protocol: FunASRProtocol = FunASRProtocol.LOCAL
    mode: str = "2pass"
    chunk_size: tuple[int, int, int] = (8, 8, 4)
    audio_fs: int = 16000
    itn: bool = True
    hotwords: tuple[str, ...] = _bundled_hotwords()
    encoder_chunk_look_back: int = 4
    decoder_chunk_look_back: int = 0
    queue_max_chunks: int = 16
    send_chunk_ms: int = 60
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if self.audio_fs <= 0:
            raise ValueError("audio_fs must be positive")
        if self.send_chunk_ms <= 0:
            raise ValueError("send_chunk_ms must be positive")
        if self.queue_max_chunks <= 0:
            raise ValueError("queue_max_chunks must be positive")
        if len(self.chunk_size) != 3 or any(value <= 0 for value in self.chunk_size):
            raise ValueError("chunk_size must contain three positive integers")
        if self.encoder_chunk_look_back < 0 or self.decoder_chunk_look_back < 0:
            raise ValueError("chunk look-back values must be non-negative")
        if self.protocol is FunASRProtocol.STANDALONE:
            if self.audio_fs != 16000:
                raise ValueError("standalone FunASR requires 16000 Hz audio")
            if self.send_chunk_ms not in {20, 60}:
                raise ValueError("standalone FunASR packets must be 20 ms or 60 ms")

    @classmethod
    def from_settings(cls, settings: Settings) -> FunASRStreamConfig:
        protocol = "local" if settings.funasr_protocol == "funasr" else settings.funasr_protocol
        return cls(
            url=settings.funasr_ws_url,
            protocol=FunASRProtocol(protocol),
            mode=settings.funasr_mode,
            chunk_size=settings.funasr_chunk_sizes,
            audio_fs=settings.funasr_audio_fs,
            queue_max_chunks=settings.funasr_queue_max_chunks,
            timeout_seconds=settings.funasr_timeout_seconds,
        )


class WebSocketConnection(Protocol):
    async def send(self, message: str | bytes) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


WebSocketFactory = Callable[[str, float], Awaitable[WebSocketConnection]]
_FlushMarker = object()
_CloseMarker = object()
_EndMarker = object()


class FunASRStream:
    """One bounded FunASR streaming recognition request.

    Audio is accepted with ``put_nowait`` semantics.  A full queue is an
    explicit recoverable error: silently growing memory would invalidate the
    latency experiment and make cancellation unreliable.
    """

    def __init__(
        self,
        config: FunASRStreamConfig,
        *,
        websocket_factory: WebSocketFactory | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self._config = config
        self._factory = websocket_factory or _open_websocket
        self._tracer = tracer
        self._audio: asyncio.Queue[bytes | object] = asyncio.Queue(maxsize=config.queue_max_chunks)
        self._events: asyncio.Queue[RecognitionEvent | object] = asyncio.Queue(maxsize=config.queue_max_chunks * 2)
        self._connection: WebSocketConnection | None = None
        self._sender: asyncio.Task[None] | None = None
        self._receiver: asyncio.Task[None] | None = None
        self._started = False
        self._closed = False
        self._fatal: ProviderError | None = None
        self._wire_is_speaking = True
        self._needs_new_segment = False
        self._events_ended = False
        self._active_segment_id = str(uuid.uuid4())
        self._finalizing_segment_id: str | None = None
        self._request_id = str(uuid.uuid4())
        self._pending_audio = bytearray()
        self._send_chunk_bytes = config.audio_fs * 2 * config.send_chunk_ms // 1000

    async def start(self) -> None:
        if self._started:
            return
        if self._tracer:
            self._tracer.event(
                "asr_connection_started",
                provider="funasr",
                request_id=self._request_id,
                protocol="local",
            )
        try:
            self._connection = await asyncio.wait_for(
                self._factory(self._config.url, self._config.timeout_seconds), timeout=self._config.timeout_seconds
            )
            await self._connection.send(json.dumps(self._initial_message(), ensure_ascii=False))
            if self._tracer:
                self._tracer.event(
                    "asr_connection_ready",
                    provider="funasr",
                    request_id=self._request_id,
                    protocol="local",
                )
        except BaseException:
            if self._connection is not None:
                try:
                    await self._connection.close()
                finally:
                    self._connection = None
            raise
        self._sender = asyncio.create_task(self._send_loop(), name="funasr-send")
        self._receiver = asyncio.create_task(self._receive_loop(), name="funasr-recv")
        self._started = True

    def push_audio(self, pcm_s16le: bytes) -> None:
        if not self._started or self._closed:
            raise RuntimeError("FunASR stream is not open")
        if not pcm_s16le:
            return
        if self._fatal:
            raise self._fatal
        if self._needs_new_segment:
            self._active_segment_id = str(uuid.uuid4())
            self._needs_new_segment = False
        self._pending_audio.extend(pcm_s16le)
        while len(self._pending_audio) >= self._send_chunk_bytes:
            chunk = bytes(self._pending_audio[: self._send_chunk_bytes])
            del self._pending_audio[: self._send_chunk_bytes]
            self._queue_audio(chunk)

    async def flush(self) -> None:
        if not self._started or self._closed:
            return
        if self._finalizing_segment_id is not None:
            raise ProviderError("funasr", "previous final transcript is still pending", retryable=True)
        self._flush_pending_audio()
        self._finalizing_segment_id = self._active_segment_id
        self._needs_new_segment = True
        try:
            await self._put_marker(_FlushMarker)
        except ProviderError:
            self._finalizing_segment_id = None
            self._needs_new_segment = False
            raise

    async def events(self) -> AsyncIterator[RecognitionEvent]:
        while True:
            item = await self._events.get()
            if item is _EndMarker:
                if self._fatal:
                    raise self._fatal
                return
            assert isinstance(item, RecognitionEvent)
            yield item

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._started:
            try:
                if not self._fatal:
                    await self._flush_pending_audio_wait()
                await self._put_marker(_CloseMarker)
                if self._sender:
                    await asyncio.wait_for(self._sender, timeout=2.0)
            except (ProviderError, TimeoutError):
                if self._sender:
                    self._sender.cancel()
            finally:
                if self._receiver:
                    self._receiver.cancel()
                await _cancel_and_wait(self._receiver)
                await _cancel_and_wait(self._sender)
                if self._connection:
                    try:
                        await self._connection.close()
                    except OSError:
                        pass
        await self._end_events()

    def _initial_message(self) -> dict[str, Any]:
        return {
            "mode": self._config.mode,
            "chunk_size": list(self._config.chunk_size),
            "chunk_interval": self._config.chunk_size[1],
            "encoder_chunk_look_back": self._config.encoder_chunk_look_back,
            "decoder_chunk_look_back": self._config.decoder_chunk_look_back,
            "wav_name": self._request_id,
            "is_speaking": True,
            "audio_fs": self._config.audio_fs,
            "itn": self._config.itn,
            # The resolved SeACo Paraformer model parses whitespace-delimited
            # terms; it does not accept the weighted JSON format used by other
            # FunASR contextual models.
            "hotwords": " ".join(self._config.hotwords),
        }

    async def _put_marker(self, marker: object) -> None:
        try:
            await asyncio.wait_for(self._audio.put(marker), timeout=self._config.timeout_seconds)
        except TimeoutError as exc:
            error = BackpressureError("funasr", "audio")
            self._set_fatal(error)
            raise error from exc

    def _flush_pending_audio(self) -> None:
        if not self._pending_audio:
            return
        chunk = bytes(self._pending_audio)
        self._pending_audio.clear()
        self._queue_audio(chunk)

    async def _flush_pending_audio_wait(self) -> None:
        if not self._pending_audio:
            return
        chunk = bytes(self._pending_audio)
        self._pending_audio.clear()
        try:
            await asyncio.wait_for(self._audio.put(chunk), timeout=self._config.timeout_seconds)
        except TimeoutError as exc:
            error = BackpressureError("funasr", "audio")
            self._set_fatal(error)
            raise error from exc

    def _queue_audio(self, chunk: bytes) -> None:
        try:
            self._audio.put_nowait(chunk)
        except asyncio.QueueFull as exc:
            error = BackpressureError("funasr", "audio")
            self._set_fatal(error)
            raise error from exc

    async def _send_loop(self) -> None:
        assert self._connection is not None
        try:
            while True:
                item = await self._audio.get()
                try:
                    if item is _FlushMarker:
                        await self._send_end_of_segment()
                        self._wire_is_speaking = False
                        continue
                    if item is _CloseMarker:
                        await self._send_end_of_segment()
                        self._wire_is_speaking = False
                        return
                    assert isinstance(item, bytes)
                    if not self._wire_is_speaking:
                        await self._connection.send(json.dumps({"is_speaking": True}))
                        self._wire_is_speaking = True
                    await self._connection.send(item)
                finally:
                    self._audio.task_done()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._set_fatal(ProviderError("funasr", redact_exception(exc), retryable=True))
            await self._end_events()

    async def _send_end_of_segment(self) -> None:
        assert self._connection is not None
        await self._connection.send(json.dumps({"is_speaking": False}))
        # The pinned FunASR server applies a control message immediately but
        # finalizes 2pass offline ASR only while processing the next PCM frame.
        await self._connection.send(bytes(self._send_chunk_bytes))

    async def _receive_loop(self) -> None:
        assert self._connection is not None
        try:
            while True:
                message = await self._connection.recv()
                if not isinstance(message, str):
                    continue
                parsed = json.loads(message)
                if not isinstance(parsed, dict):
                    continue
                event = self._parse_message(parsed)
                if event is not None:
                    try:
                        await asyncio.wait_for(self._events.put(event), timeout=self._config.timeout_seconds)
                    except TimeoutError as exc:
                        self._set_fatal(BackpressureError("funasr", "event"))
                        raise exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closed:
                self._set_fatal(ProviderError("funasr", redact_exception(exc), retryable=True))
            await self._end_events()

    def _parse_message(self, message: dict[str, Any]) -> RecognitionEvent | None:
        mode = message.get("mode")
        text = message.get("text")
        if mode == "2pass-offline":
            # An empty offline result still closes the preceding flush. Leaving
            # this marker set would make every later turn fail as "pending".
            segment_id = self._finalizing_segment_id or self._active_segment_id
            self._finalizing_segment_id = None
            if not isinstance(text, str) or not text.strip():
                return None
            return RecognitionEvent(
                kind=RecognitionKind.FINAL,
                text=text,
                segment_id=segment_id,
                request_id=self._request_id,
                raw_mode=mode,
            )
        if mode == "2pass-online" and isinstance(text, str) and text.strip():
            return RecognitionEvent(
                kind=RecognitionKind.INTERIM,
                text=text,
                segment_id=self._active_segment_id,
                request_id=self._request_id,
                raw_mode=mode,
            )
        return None

    def _set_fatal(self, error: ProviderError) -> None:
        if self._fatal is None:
            self._fatal = error
            if self._tracer:
                self._tracer.event("asr_error", provider="funasr", retryable=error.retryable)

    async def _end_events(self) -> None:
        if self._events_ended:
            return
        self._events_ended = True
        try:
            self._events.put_nowait(_EndMarker)
        except asyncio.QueueFull:
            # The stream has already failed due to a slow consumer.  Evicting one
            # queued event guarantees that cancellation cannot wait forever.
            self._events.get_nowait()
            self._events.put_nowait(_EndMarker)


class StandaloneFunASRStream:
    """Remote FunASR transport using one WebSocket connection per utterance."""

    def __init__(
        self,
        config: FunASRStreamConfig,
        *,
        websocket_factory: WebSocketFactory | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        if config.protocol is not FunASRProtocol.STANDALONE:
            raise ValueError("StandaloneFunASRStream requires the standalone protocol")
        self._config = config
        self._factory = websocket_factory or _open_websocket
        self._tracer = tracer
        self._audio: asyncio.Queue[bytes | object] = asyncio.Queue(maxsize=config.queue_max_chunks)
        self._events: asyncio.Queue[RecognitionEvent | object] = asyncio.Queue(maxsize=config.queue_max_chunks * 2)
        self._pending_audio = bytearray()
        self._send_chunk_bytes = config.audio_fs * 2 * config.send_chunk_ms // 1000
        self._minimum_packet_bytes = config.audio_fs * 2 * 20 // 1000
        self._runner: asyncio.Task[None] | None = None
        self._connection: WebSocketConnection | None = None
        self._receiver: asyncio.Task[None] | None = None
        self._started = False
        self._closed = False
        self._fatal: ProviderError | None = None
        self._events_ended = False

    async def start(self) -> None:
        if self._started:
            return
        self._runner = asyncio.create_task(self._run(), name="funasr-standalone")
        self._started = True

    def push_audio(self, pcm_s16le: bytes) -> None:
        if not self._started or self._closed:
            raise RuntimeError("FunASR stream is not open")
        if self._fatal:
            raise self._fatal
        if not pcm_s16le:
            return
        self._pending_audio.extend(pcm_s16le)
        while len(self._pending_audio) >= self._send_chunk_bytes:
            chunk = bytes(self._pending_audio[: self._send_chunk_bytes])
            del self._pending_audio[: self._send_chunk_bytes]
            self._queue_audio(chunk)

    async def push_audio_wait(self, pcm_s16le: bytes) -> None:
        if not self._started or self._closed:
            raise RuntimeError("FunASR stream is not open")
        if self._fatal:
            raise self._fatal
        if not pcm_s16le:
            return
        self._pending_audio.extend(pcm_s16le)
        while len(self._pending_audio) >= self._send_chunk_bytes:
            chunk = bytes(self._pending_audio[: self._send_chunk_bytes])
            await self._put_audio_wait(chunk)
            del self._pending_audio[: self._send_chunk_bytes]

    async def flush(self) -> None:
        if not self._started or self._closed:
            return
        await self._queue_padded_tail_wait()
        await self._put_marker(_FlushMarker)

    async def events(self) -> AsyncIterator[RecognitionEvent]:
        while True:
            item = await self._events.get()
            if item is _EndMarker:
                if self._fatal:
                    raise self._fatal
                return
            assert isinstance(item, RecognitionEvent)
            yield item

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._started and self._runner:
            try:
                if not self._fatal:
                    self._queue_padded_tail()
                await self._put_marker(_CloseMarker)
                await asyncio.wait_for(self._runner, timeout=min(self._config.timeout_seconds, 2.0))
            except (ProviderError, TimeoutError):
                self._runner.cancel()
                await _cancel_and_wait(self._runner)
            finally:
                await self._close_segment()
        await self._end_events()

    def _queue_padded_tail(self) -> None:
        if not self._pending_audio:
            return
        target = (
            self._minimum_packet_bytes
            if len(self._pending_audio) <= self._minimum_packet_bytes
            else self._send_chunk_bytes
        )
        self._pending_audio.extend(bytes(target - len(self._pending_audio)))
        chunk = bytes(self._pending_audio)
        self._pending_audio.clear()
        self._queue_audio(chunk)

    async def _queue_padded_tail_wait(self) -> None:
        if not self._pending_audio:
            return
        target = (
            self._minimum_packet_bytes
            if len(self._pending_audio) <= self._minimum_packet_bytes
            else self._send_chunk_bytes
        )
        chunk = bytes(self._pending_audio) + bytes(target - len(self._pending_audio))
        await self._put_audio_wait(chunk)
        self._pending_audio.clear()

    def _queue_audio(self, chunk: bytes) -> None:
        try:
            self._audio.put_nowait(chunk)
        except asyncio.QueueFull as exc:
            error = BackpressureError("funasr", "audio")
            self._set_fatal(error)
            raise error from exc

    async def _put_audio_wait(self, chunk: bytes) -> None:
        try:
            await asyncio.wait_for(self._audio.put(chunk), timeout=self._config.timeout_seconds)
        except TimeoutError as exc:
            error = BackpressureError("funasr", "audio")
            self._set_fatal(error)
            raise error from exc
        if self._fatal:
            raise self._fatal

    async def _put_marker(self, marker: object) -> None:
        try:
            await asyncio.wait_for(self._audio.put(marker), timeout=self._config.timeout_seconds)
        except TimeoutError as exc:
            error = BackpressureError("funasr", "audio")
            self._set_fatal(error)
            raise error from exc

    async def _run(self) -> None:
        try:
            while True:
                item = await self._audio.get()
                try:
                    if item is _FlushMarker or item is _CloseMarker:
                        if self._connection is not None:
                            await self._finish_segment()
                        if item is _CloseMarker:
                            return
                        continue
                    assert isinstance(item, bytes)
                    await self._ensure_segment()
                    if self._receiver and self._receiver.done():
                        self._receiver.result()
                    assert self._connection is not None
                    await self._connection.send(item)
                finally:
                    self._audio.task_done()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = (
                exc
                if isinstance(exc, ProviderError)
                else ProviderError("funasr", redact_exception(exc), retryable=True)
            )
            self._set_fatal(error)
        finally:
            await self._close_segment()
            await self._end_events()

    async def _ensure_segment(self) -> None:
        if self._connection is not None:
            return
        segment_id = str(uuid.uuid4())
        request_id = str(uuid.uuid4())
        turn_id = self._tracer.ensure_turn() if self._tracer else None
        if self._tracer:
            self._tracer.event(
                "asr_request_started",
                provider="funasr",
                request_id=request_id,
                segment_id=segment_id,
                turn_id=turn_id,
            )
        self._connection = await asyncio.wait_for(
            self._factory(self._config.url, self._config.timeout_seconds),
            timeout=self._config.timeout_seconds,
        )
        try:
            await self._connection.send(json.dumps(self._start_message(), ensure_ascii=False))
            if self._tracer:
                self._tracer.event(
                    "asr_stream_ready",
                    provider="funasr",
                    request_id=request_id,
                    segment_id=segment_id,
                    turn_id=turn_id,
                )
        except BaseException:
            await self._close_segment()
            raise
        self._receiver = asyncio.create_task(
            self._receive_segment(self._connection, segment_id, request_id),
            name="funasr-standalone-recv",
        )

    def _start_message(self) -> dict[str, Any]:
        return {
            "type": "start",
            "sample_rate_hz": self._config.audio_fs,
            "channels": 1,
            "codec": "pcm16le",
            "language": "zh",
            "hotwords": list(self._config.hotwords),
        }

    async def _finish_segment(self) -> None:
        assert self._connection is not None
        assert self._receiver is not None
        await self._connection.send(json.dumps({"type": "finish"}))
        await asyncio.wait_for(self._receiver, timeout=self._config.timeout_seconds)
        await self._close_segment()

    async def _receive_segment(
        self,
        connection: WebSocketConnection,
        segment_id: str,
        request_id: str,
    ) -> None:
        last_normalized_interim: str | None = None
        while True:
            message = await connection.recv()
            if not isinstance(message, str):
                continue
            parsed = json.loads(message)
            if not isinstance(parsed, dict):
                continue
            message_type = parsed.get("type")
            if message_type == "error":
                code = parsed.get("code")
                detail = parsed.get("error")
                if code == "invalid_audio" and detail == "FunASR returned empty text":
                    await self._put_event(
                        RecognitionEvent(
                            kind=RecognitionKind.FINAL,
                            text="",
                            segment_id=segment_id,
                            request_id=request_id,
                            raw_mode="standalone-empty-final",
                        )
                    )
                    return
                known_codes = {
                    "already_started",
                    "busy",
                    "inference_failed",
                    "invalid_audio",
                    "invalid_event",
                    "invalid_json",
                    "invalid_start",
                    "start_required",
                    "streaming_disabled",
                }
                safe_code = code if isinstance(code, str) and code in known_codes else "unknown"
                retryable = safe_code in {"busy", "inference_failed"}
                raise ProviderError(
                    "funasr",
                    f"standalone ASR rejected the stream ({safe_code})",
                    retryable=retryable,
                )
            if message_type not in {"interim", "final"}:
                continue
            text = parsed.get("text")
            if isinstance(text, str) and (text.strip() or message_type == "final"):
                normalized_text = " ".join(text.split())
                if message_type == "interim" and normalized_text == last_normalized_interim:
                    if self._tracer:
                        self._tracer.event(
                            "asr_interim_deduplicated",
                            provider="funasr",
                            request_id=request_id,
                            segment_id=segment_id,
                            text_length=len(normalized_text),
                            text_hash=hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()[:12],
                        )
                    continue
                if message_type == "interim":
                    last_normalized_interim = normalized_text
                await self._put_event(
                    RecognitionEvent(
                        kind=RecognitionKind.FINAL if message_type == "final" else RecognitionKind.INTERIM,
                        text=text if normalized_text else "",
                        segment_id=segment_id,
                        request_id=request_id,
                        raw_mode=f"standalone-{message_type}",
                    )
                )
            if message_type == "final":
                return

    async def _put_event(self, event: RecognitionEvent) -> None:
        try:
            await asyncio.wait_for(self._events.put(event), timeout=self._config.timeout_seconds)
        except TimeoutError as exc:
            raise BackpressureError("funasr", "event") from exc

    async def _close_segment(self) -> None:
        receiver = self._receiver
        connection = self._connection
        self._receiver = None
        self._connection = None
        await _cancel_and_wait(receiver)
        if connection is not None:
            try:
                await connection.close()
            except OSError:
                pass

    def _set_fatal(self, error: ProviderError) -> None:
        if self._fatal is None:
            self._fatal = error
            if self._tracer:
                self._tracer.event("asr_error", provider="funasr", retryable=error.retryable)

    async def _end_events(self) -> None:
        if self._events_ended:
            return
        self._events_ended = True
        try:
            self._events.put_nowait(_EndMarker)
        except asyncio.QueueFull:
            self._events.get_nowait()
            self._events.put_nowait(_EndMarker)


async def _cancel_and_wait(task: asyncio.Task[None] | None) -> None:
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _open_websocket(url: str, timeout_seconds: float) -> WebSocketConnection:
    from websockets.asyncio.client import connect

    return await connect(url, subprotocols=["binary"], open_timeout=timeout_seconds, close_timeout=1.0)


class FunASRSTT(stt.STT):
    """LiveKit Agents 1.6.5 adapter with intentionally unaligned transcripts."""

    def __init__(
        self,
        config: FunASRStreamConfig,
        *,
        tracer: Tracer | None = None,
        recording_dir: Path | None = None,
        recording_room: str | None = None,
    ) -> None:
        supports_streaming = config.protocol is FunASRProtocol.LOCAL
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=supports_streaming,
                interim_results=supports_streaming,
                aligned_transcript=False,
                offline_recognize=not supports_streaming,
            )
        )
        self._config = config
        self._tracer = tracer
        self._recording_dir = recording_dir
        self._recording_room = recording_room

    @property
    def model(self) -> str:
        return "paraformer-zh-streaming-2pass"

    @property
    def provider(self) -> str:
        return "funasr"

    async def _recognize_impl(self, buffer: Any, *, language: Any, conn_options: Any) -> stt.SpeechEvent:
        del conn_options
        if self._config.protocol is FunASRProtocol.LOCAL:
            raise NotImplementedError("FunASR adapter only supports streaming recognition")

        frame = utils.combine_frames(buffer)
        if frame.num_channels != 1:
            raise ValueError("standalone FunASR requires mono audio")
        if frame.sample_rate <= 0:
            raise ValueError("standalone FunASR requires a positive sample rate")
        if frame.sample_rate == self._config.audio_fs:
            pcm_s16le = bytes(frame.data)
        else:
            resampler = rtc.AudioResampler(
                input_rate=frame.sample_rate,
                output_rate=self._config.audio_fs,
                num_channels=1,
            )
            resampled_frames = resampler.push(frame)
            resampled_frames.extend(resampler.flush())
            pcm_s16le = b"".join(bytes(output.data) for output in resampled_frames)

        stream = StandaloneFunASRStream(self._config, tracer=self._tracer)
        recorder = (
            AsrWavRecorder(
                self._recording_dir,
                sample_rate=self._config.audio_fs,
                room=self._recording_room,
                on_saved=self._recording_saved,
                on_error=self._recording_error,
            )
            if self._recording_dir is not None
            else None
        )
        try:
            if recorder is not None:
                recorder.write(pcm_s16le)
            await stream.start()
            await stream.push_audio_wait(pcm_s16le)
            await stream.flush()
            events = stream.events()
            while True:
                event = await asyncio.wait_for(anext(events), timeout=self._config.timeout_seconds)
                self._trace_provider_event(event)
                if event.kind is RecognitionKind.FINAL:
                    return stt.SpeechEvent(
                        type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                        request_id=event.request_id,
                        alternatives=[
                            stt.SpeechData(
                                language=language if isinstance(language, str) else "zh-CN",
                                text=event.text,
                            )
                        ],
                    )
        except StopAsyncIteration as exc:
            raise APIConnectionError("funasr: standalone ASR returned no final transcript", retryable=True) from exc
        except ProviderError as exc:
            raise APIConnectionError(str(exc), retryable=exc.retryable) from exc
        except (OSError, TimeoutError) as exc:
            raise APIConnectionError(
                f"funasr: {redact_exception(exc)}",
                retryable=True,
            ) from exc
        finally:
            await stream.aclose()
            if recorder is not None:
                recorder.close()

    def _trace_provider_event(self, event: RecognitionEvent) -> None:
        if self._tracer is None:
            return
        turn_id = self._tracer.ensure_turn()
        self._tracer.event(
            "asr_provider_final" if event.kind is RecognitionKind.FINAL else "asr_provider_interim",
            provider="funasr",
            request_id=event.request_id,
            segment_id=event.segment_id,
            turn_id=turn_id,
        )

    def _recording_saved(self, path: Path) -> None:
        if self._tracer is not None:
            self._tracer.event("asr_audio_recording_saved", filename=path.name)

    def _recording_error(self) -> None:
        if self._tracer is not None:
            self._tracer.event("asr_audio_recording_error")

    def stream(
        self, *, language: Any = "zh-CN", conn_options: Any = DEFAULT_API_CONNECT_OPTIONS
    ) -> stt.RecognizeStream:
        return _FunASRRecognizeStream(
            stt_instance=self,
            config=self._config,
            language=language if isinstance(language, str) else "zh-CN",
            conn_options=conn_options,
            tracer=self._tracer,
            recording_dir=self._recording_dir,
            recording_room=self._recording_room,
        )


class _FunASRRecognizeStream(stt.RecognizeStream):
    def __init__(
        self,
        *,
        stt_instance: FunASRSTT,
        config: FunASRStreamConfig,
        language: str,
        conn_options: Any,
        tracer: Tracer | None,
        recording_dir: Path | None,
        recording_room: str | None,
    ) -> None:
        super().__init__(stt=stt_instance, conn_options=conn_options, sample_rate=config.audio_fs)
        self._config = config
        self._language = language
        self._tracer = tracer
        self._recorder = (
            AsrWavRecorder(
                recording_dir,
                sample_rate=config.audio_fs,
                room=recording_room,
                on_saved=self._recording_saved,
                on_error=self._recording_error,
            )
            if recording_dir is not None
            else None
        )

    async def _run(self) -> None:
        stream = (
            StandaloneFunASRStream(self._config, tracer=self._tracer)
            if self._config.protocol is FunASRProtocol.STANDALONE
            else FunASRStream(self._config, tracer=self._tracer)
        )
        input_task: asyncio.Task[None] | None = None
        events_task: asyncio.Task[None] | None = None
        try:
            await stream.start()
            input_task = asyncio.create_task(self._forward_input(stream), name="funasr-livekit-input")
            events_task = asyncio.create_task(self._forward_events(stream), name="funasr-livekit-events")
            done, _ = await asyncio.wait({input_task, events_task}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            if input_task in done:
                await events_task
            else:
                input_task.cancel()
                await _cancel_and_wait(input_task)
        except ProviderError as exc:
            # LiveKit retries streaming STT only for its public APIError family.
            # Preserve the provider's retryability instead of closing AgentSession.
            raise APIConnectionError(str(exc), retryable=exc.retryable) from exc
        except (OSError, TimeoutError) as exc:
            raise APIConnectionError(
                f"funasr: {redact_exception(exc)}",
                retryable=True,
            ) from exc
        finally:
            await stream.aclose()
            if self._recorder is not None:
                self._recorder.close()
            await _cancel_and_wait(input_task)
            await _cancel_and_wait(events_task)

    async def _forward_input(self, stream: FunASRStream) -> None:
        async for item in self._input_ch:
            if isinstance(item, self._FlushSentinel):
                await stream.flush()
                continue
            pcm_s16le = bytes(item.data)
            if self._recorder is not None:
                self._recorder.write(pcm_s16le)
            stream.push_audio(pcm_s16le)
        await stream.aclose()

    async def _forward_events(self, stream: FunASRStream) -> None:
        async for event in stream.events():
            if self._tracer is not None:
                turn_id = self._tracer.ensure_turn()
                self._tracer.event(
                    "asr_provider_final" if event.kind is RecognitionKind.FINAL else "asr_provider_interim",
                    provider="funasr",
                    request_id=event.request_id,
                    segment_id=event.segment_id,
                    turn_id=turn_id,
                )
            event_type = (
                stt.SpeechEventType.INTERIM_TRANSCRIPT
                if event.kind is RecognitionKind.INTERIM
                else stt.SpeechEventType.FINAL_TRANSCRIPT
            )
            self._event_ch.send_nowait(
                stt.SpeechEvent(
                    type=event_type,
                    request_id=event.request_id,
                    alternatives=[stt.SpeechData(language=self._language, text=event.text)],
                )
            )

    def _recording_saved(self, path: Path) -> None:
        if self._tracer is not None:
            self._tracer.event("asr_audio_recording_saved", filename=path.name)

    def _recording_error(self) -> None:
        if self._tracer is not None:
            self._tracer.event("asr_audio_recording_error")
