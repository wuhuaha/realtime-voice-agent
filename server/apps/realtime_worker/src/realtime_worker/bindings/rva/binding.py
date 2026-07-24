"""Strict rva-control-v2 binding with server-owned response state."""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from .protocol import (
    CONTROL_MAX_BYTES,
    MEDIA_MAX_PAYLOAD_BYTES,
    UDP_PROFILE,
    WSS_PROFILE,
    RvaBindingError,
    WssMediaFrame,
    decode_control,
    encode_control,
    parse_session_open_object,
    require_exact_fields,
    require_identifier,
    require_uint32,
)
from .response import PlaybackEndedFact, ResponseCoordinator, ResponseRecord


@dataclass(frozen=True, slots=True)
class InboundAudioPacket:
    sequence: int
    timestamp: int
    payload: bytes


class AudioInputPort(Protocol):
    async def receive_audio(self, packet: InboundAudioPacket) -> None: ...

    async def close(self) -> None: ...


class AgentControlPort(Protocol):
    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ControlEffect:
    outbound: tuple[str, ...] = ()
    interrupt: ResponseRecord | None = None
    playback_started: ResponseRecord | None = None
    playback_ended: PlaybackEndedFact | None = None
    close_after_send: bool = False


class RvaWssBinding:
    """One immutable rva-control-v2 session and its response coordinator."""

    def __init__(
        self,
        *,
        expected_device_id: str,
        session_id: str,
        session_epoch: str,
        media_id: bytes,
        media_epoch: int,
        allowed_profiles: frozenset[str] = frozenset({WSS_PROFILE}),
        udp_grant: Mapping[str, object] | None = None,
        audio_port: AudioInputPort,
        agent_port: AgentControlPort,
        close_stage_timeout_seconds: float = 2.0,
    ) -> None:
        self._expected_device_id = require_identifier(expected_device_id, "device_id")
        self._session_id = require_identifier(session_id, "session_id")
        self._session_epoch = require_identifier(session_epoch, "session_epoch")
        if not isinstance(media_id, bytes) or len(media_id) != 8:
            raise RvaBindingError("invalid_media_id")
        self._media_id = media_id
        self._media_epoch = require_uint32(media_epoch, "media_epoch", minimum=1)
        self._allowed_profiles = allowed_profiles
        self._udp_grant = dict(udp_grant) if udp_grant is not None else None
        self._audio_port = audio_port
        self._responses = ResponseCoordinator(
            connection_epoch=self._session_epoch,
            close_ports=(audio_port, agent_port),
            close_timeout=close_stage_timeout_seconds,
        )
        self._opened = False
        self._selected_media_profile: str | None = None
        self._next_uplink_sequence = 0
        self._next_downlink_sequence = 0
        self._next_response_text_sequence = 0
        self._active_utterance_id: str | None = None
        self._next_transcript_sequence = 0
        self._last_finalized_utterance: str | None = None
        self._cancel_requests: dict[str, tuple[str, int, str]] = {}
        self._cancel_request_order: deque[str] = deque()

    @property
    def opened(self) -> bool:
        return self._opened

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def session_epoch(self) -> str:
        return self._session_epoch

    @property
    def closed(self) -> bool:
        return self._responses.closed

    @property
    def active_response(self) -> ResponseRecord | None:
        return self._responses.active

    @property
    def active_playback(self):  # type: ignore[no-untyped-def]
        playback = self._responses.playback
        return playback.target if playback is not None else None

    @property
    def current_playback(self) -> ResponseRecord | None:
        return self._responses.playback

    @property
    def selected_media_profile(self) -> str | None:
        return self._selected_media_profile

    @property
    def uplink_generation(self) -> int:
        return 0

    async def receive_control(self, raw: str) -> ControlEffect:
        message = decode_control(raw)
        message_type = message.get("type")
        if message_type == "session.open":
            return ControlEffect((self._open(message),))
        self._ensure_open()
        if message_type == "response.cancel.request":
            return await self._cancel(message)
        if message_type == "playback.started":
            return self._playback_started(message)
        if message_type == "playback.ended":
            return await self._playback_ended(message)
        if message_type == "session.close":
            return await self._receive_close(message)
        raise RvaBindingError("unknown_client_message")

    async def cancel_active_response(self, *, cause: str) -> ControlEffect:
        self._ensure_open()
        record = self._responses.playback
        if record is None:
            return ControlEffect()
        if cause not in {"explicit_user_request", "recognized_interrupt", "session_close", "response_failed"}:
            raise RvaBindingError("invalid_stop_cause")
        outcome = "failed" if cause == "response_failed" else "cancelled"
        error_code = "response_failed" if outcome == "failed" else None
        fence = await self._responses.fence(record, outcome=outcome, error_code=error_code)
        stop = self._event(
            "playback.stop",
            target={"response_id": record.response_id, "generation": record.target.generation},
            fence_generation=fence.fence_generation,
            cause=cause,
        )
        outbound = [stop]
        if record.outcome != "completed":
            end_fields: dict[str, object] = {
                "response_id": record.response_id,
                "generation": record.target.generation,
                "outcome": outcome,
            }
            if error_code is not None:
                end_fields["error_code"] = error_code
            outbound.append(self._event("response.end", **end_fields))
        return ControlEffect(tuple(outbound), interrupt=record)

    async def receive_media(self, raw: bytes) -> None:
        self._ensure_open()
        if self._selected_media_profile != WSS_PROFILE:
            raise RvaBindingError("transport_mismatch")
        frame = WssMediaFrame.parse(raw)
        if frame.media_id != self._media_id or frame.media_epoch != self._media_epoch:
            raise RvaBindingError("stale_media_identity")
        if frame.generation != 0:
            raise RvaBindingError("invalid_uplink_generation")
        if frame.sequence != self._next_uplink_sequence:
            raise RvaBindingError("invalid_media_sequence")
        self._next_uplink_sequence += 1
        await self._audio_port.receive_audio(InboundAudioPacket(frame.sequence, frame.timestamp, frame.payload))

    def serialize_audio(self, payload: bytes, *, timestamp: int, record: ResponseRecord) -> bytes:
        self._ensure_open()
        if self._selected_media_profile != WSS_PROFILE:
            raise RvaBindingError("transport_mismatch")
        sequence = self.reserve_downlink_media(record)
        return WssMediaFrame(
            media_id=self._media_id,
            media_epoch=self._media_epoch,
            sequence=sequence,
            timestamp=require_uint32(timestamp, "timestamp"),
            generation=record.target.generation,
            payload=payload,
        ).serialize()

    def reserve_downlink_media(self, record: ResponseRecord) -> int:
        if self._next_downlink_sequence > 0xFFFFFFFF:
            raise RvaBindingError("media_sequence_exhausted")
        sequence = self._next_downlink_sequence
        self._responses.note_media(record, sequence)
        self._next_downlink_sequence += 1
        return sequence

    def note_downlink_media(self, record: ResponseRecord, sequence: int) -> None:
        sequence = require_uint32(sequence, "sequence")
        self._responses.note_media(record, sequence)

    def transcript_delta(self, *, utterance_id: str, sequence: int, text: str) -> str:
        self._ensure_open()
        utterance_id = require_identifier(utterance_id, "utterance_id")
        sequence = require_uint32(sequence, "sequence")
        self._require_text(text, maximum_bytes=4_096, allow_empty=False)
        self._advance_transcript(utterance_id, sequence, final=False)
        return self._event("transcript.delta", utterance_id=utterance_id, sequence=sequence, text=text)

    def transcript_final(self, *, utterance_id: str, sequence: int, text: str) -> str:
        self._ensure_open()
        utterance_id = require_identifier(utterance_id, "utterance_id")
        sequence = require_uint32(sequence, "sequence")
        self._require_text(text, maximum_bytes=16_384, allow_empty=True)
        self._advance_transcript(utterance_id, sequence, final=True)
        return self._event("transcript.final", utterance_id=utterance_id, sequence=sequence, text=text)

    async def response_begin(self, *, response_id: str, producer_epoch: int) -> tuple[ResponseRecord, str | None]:
        self._ensure_open()
        response_id = require_identifier(response_id, "response_id")
        record, created = await self._responses.begin(response_id, producer_epoch)
        if not created:
            return record, None
        self._next_response_text_sequence = 0
        return record, self._event(
            "response.begin",
            response_id=record.response_id,
            generation=record.target.generation,
        )

    def response_text(self, *, record: ResponseRecord, sequence: int, text: str) -> str:
        self._ensure_open()
        if not self._responses.accepts(record):
            raise RvaBindingError("stale_generation")
        sequence = require_uint32(sequence, "sequence")
        if sequence != self._next_response_text_sequence:
            raise RvaBindingError("invalid_sequence")
        self._require_text(text, maximum_bytes=4_096, allow_empty=False)
        self._next_response_text_sequence += 1
        return self._event(
            "response.text",
            response_id=record.response_id,
            generation=record.target.generation,
            sequence=sequence,
            text=text,
        )

    async def response_end(self, *, record: ResponseRecord) -> str:
        self._ensure_open()
        await self._responses.complete(record)
        assert record.last_media_sequence is not None
        return self._event(
            "response.end",
            response_id=record.response_id,
            generation=record.target.generation,
            outcome="completed",
            final_media_sequence=record.last_media_sequence,
        )

    def session_error(self, *, code: str, retryable: bool, message: str) -> str:
        self._ensure_open()
        if not isinstance(code, str) or not is_valid_error_code(code):
            raise RvaBindingError("invalid_error_code")
        if type(retryable) is not bool:
            raise RvaBindingError("invalid_retryable")
        self._require_text(message, maximum_bytes=512, allow_empty=True)
        return self._event("session.error", code=code, retryable=retryable, message=message)

    async def close(self) -> None:
        await self._responses.close()

    def _open(self, message: dict[str, object]) -> str:
        if self._opened:
            raise RvaBindingError("duplicate_session_open")
        if self.closed:
            raise RvaBindingError("session_closed")
        opened = parse_session_open_object(message)
        if opened.device_id != self._expected_device_id:
            raise RvaBindingError("device_id_mismatch")
        offered = tuple(profile for profile in opened.supported_media_profiles if profile in self._allowed_profiles)
        if not offered:
            raise RvaBindingError("unsupported_media_profile")
        selected = opened.preferred_media_profile if opened.preferred_media_profile in offered else (
            WSS_PROFILE if WSS_PROFILE in offered else offered[0]
        )
        if selected == UDP_PROFILE and self._udp_grant is None:
            raise RvaBindingError("udp_unavailable")
        self._opened = True
        self._selected_media_profile = selected
        response: dict[str, object] = {
            "type": "session.opened",
            "request_id": opened.request_id,
            "session_id": self._session_id,
            "session_epoch": self._session_epoch,
            "media_id": self._media_id.hex(),
            "media_epoch": self._media_epoch,
            "selected_media_profile": selected,
            "audio": {"codec": "opus", "sample_rate_hz": 16_000, "channels": 1, "frame_duration_ms": 60},
            "heartbeat_interval_ms": 15_000,
            "idle_timeout_ms": 45_000,
            "max_control_message_bytes": CONTROL_MAX_BYTES,
        }
        if selected == UDP_PROFILE:
            response["udp_grant"] = self._udp_grant
        return encode_control(response)

    async def _cancel(self, message: dict[str, object]) -> ControlEffect:
        require_exact_fields(message, {"type", "session_id", "session_epoch", "request_id", "target", "cause"})
        self._require_context(message)
        request_id = require_identifier(message["request_id"], "request_id")
        if message["cause"] != "user_request":
            raise RvaBindingError("invalid_cancel_cause")
        response_id, generation = self._parse_target(message["target"])
        identity = (response_id, generation, "user_request")
        previous = self._cancel_requests.get(request_id)
        if previous is not None:
            if previous != identity:
                raise RvaBindingError("request_id_conflict")
            return ControlEffect()
        active = self._responses.playback
        if active is None or active.response_id != response_id or active.target.generation != generation:
            raise RvaBindingError("stale_cancel_target")
        effect = await self.cancel_active_response(cause="explicit_user_request")
        self._remember_cancel_request(request_id, identity)
        return effect

    def _remember_cancel_request(self, request_id: str, identity: tuple[str, int, str]) -> None:
        self._cancel_requests[request_id] = identity
        self._cancel_request_order.append(request_id)
        while len(self._cancel_request_order) > 64:
            expired = self._cancel_request_order.popleft()
            self._cancel_requests.pop(expired, None)

    def _playback_started(self, message: dict[str, object]) -> ControlEffect:
        require_exact_fields(message, {"type", "session_id", "session_epoch", "target", "first_media_sequence"})
        self._require_context(message)
        response_id, generation = self._parse_target(message["target"])
        sequence = require_uint32(message["first_media_sequence"], "first_media_sequence")
        record = self._responses.playback_started(response_id, generation, sequence)
        return ControlEffect(playback_started=record)

    async def _playback_ended(self, message: dict[str, object]) -> ControlEffect:
        required = {"type", "session_id", "session_epoch", "target", "outcome", "played_samples"}
        actual = set(message)
        if actual != required and actual != required | {"last_media_sequence"}:
            raise RvaBindingError("missing_field" if required - actual else "unknown_field")
        self._require_context(message)
        response_id, generation = self._parse_target(message["target"])
        outcome = message["outcome"]
        if outcome not in {"completed", "stopped", "failed"}:
            raise RvaBindingError("invalid_playback_outcome")
        played_samples = self._require_uint53(message["played_samples"], "played_samples")
        last = message.get("last_media_sequence")
        last_sequence = require_uint32(last, "last_media_sequence") if last is not None else None
        if outcome == "completed" and last_sequence is None:
            raise RvaBindingError("missing_field")
        fact = await self._responses.playback_ended(
            response_id,
            generation,
            outcome=outcome,
            played_samples=played_samples,
            last_media_sequence=last_sequence,
        )
        return ControlEffect(playback_ended=fact)

    async def _receive_close(self, message: dict[str, object]) -> ControlEffect:
        required = {"type", "session_id", "session_epoch", "reason", "initiated_by"}
        actual = set(message)
        if actual != required and actual != required | {"detail"}:
            raise RvaBindingError("missing_field" if required - actual else "unknown_field")
        self._require_context(message)
        if message["initiated_by"] != "device":
            raise RvaBindingError("invalid_close_initiator")
        if message["reason"] not in {"normal", "idle_timeout", "network_change", "protocol_error"}:
            raise RvaBindingError("invalid_close_reason")
        if "detail" in message:
            self._require_text(message["detail"], maximum_bytes=256, allow_empty=True)
        cancelled = await self.cancel_active_response(cause="session_close")
        close = self._event("session.close", reason=message["reason"], initiated_by="server")
        return ControlEffect(cancelled.outbound + (close,), interrupt=cancelled.interrupt, close_after_send=True)

    def _parse_target(self, value: object) -> tuple[str, int]:
        if not isinstance(value, dict):
            raise RvaBindingError("invalid_cancel_target")
        require_exact_fields(value, {"response_id", "generation"})
        return (
            require_identifier(value["response_id"], "response_id"),
            require_uint32(value["generation"], "generation", minimum=1),
        )

    def _require_context(self, message: dict[str, object]) -> None:
        if message.get("session_id") != self._session_id or message.get("session_epoch") != self._session_epoch:
            raise RvaBindingError("stale_session")

    def _advance_transcript(self, utterance_id: str, sequence: int, *, final: bool) -> None:
        if utterance_id == self._last_finalized_utterance:
            raise RvaBindingError("transcript_already_final")
        if self._active_utterance_id is None:
            if sequence != 0:
                raise RvaBindingError("invalid_sequence")
            self._active_utterance_id = utterance_id
            self._next_transcript_sequence = 0
        if utterance_id != self._active_utterance_id or sequence != self._next_transcript_sequence:
            raise RvaBindingError("invalid_sequence")
        self._next_transcript_sequence += 1
        if final:
            self._last_finalized_utterance = utterance_id
            self._active_utterance_id = None
            self._next_transcript_sequence = 0

    def _event(self, event_type: str, **fields: object) -> str:
        return encode_control(
            {"type": event_type, "session_id": self._session_id, "session_epoch": self._session_epoch, **fields}
        )

    def _ensure_open(self) -> None:
        if self.closed:
            raise RvaBindingError("session_closed")
        if not self._opened:
            raise RvaBindingError("session_not_open")

    @staticmethod
    def _require_uint53(value: object, field: str) -> int:
        if type(value) is not int or not 0 <= value <= 9_007_199_254_740_991:
            raise RvaBindingError(f"invalid_{field}")
        return value

    @staticmethod
    def _require_text(value: object, *, maximum_bytes: int, allow_empty: bool) -> None:
        if not isinstance(value, str) or (not allow_empty and not value):
            raise RvaBindingError("invalid_text")
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise RvaBindingError("invalid_text") from exc
        if len(encoded) > maximum_bytes:
            raise RvaBindingError("text_too_large")


def is_valid_error_code(value: str) -> bool:
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789_"
    return 1 <= len(value) <= 64 and all(character in allowed for character in value)


__all__ = [
    "AgentControlPort",
    "AudioInputPort",
    "ControlEffect",
    "InboundAudioPacket",
    "MEDIA_MAX_PAYLOAD_BYTES",
    "RvaWssBinding",
]
