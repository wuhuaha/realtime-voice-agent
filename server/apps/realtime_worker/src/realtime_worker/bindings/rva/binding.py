"""Transport-only WSS binding for the canonical realtime voice session."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from realtime_worker.voice.session import CancelDisposition, PlaybackRef, VoiceSessionState

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


@dataclass(frozen=True, slots=True)
class InboundAudioPacket:
    sequence: int
    timestamp: int
    payload: bytes


class AudioInputPort(Protocol):
    async def receive_audio(self, packet: InboundAudioPacket) -> None: ...

    async def close(self) -> None: ...


class AgentControlPort(Protocol):
    async def interrupt(self, target: PlaybackRef) -> None: ...

    async def close(self) -> None: ...


class RvaWssBinding:
    """One WSS connection with one immutable rva-control-v1 session identity."""

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
        self._state = VoiceSessionState(
            agent_port,
            close_ports=(audio_port, agent_port),
            connection_epoch=self._session_epoch,
        )
        self._opened = False
        self._selected_media_profile: str | None = None
        self._client_request_id: str | None = None
        self._next_uplink_sequence = 0
        self._next_downlink_sequence = 0
        self._active_response_id: str | None = None
        self._active_response: PlaybackRef | None = None
        self._next_response_text_sequence = 0
        self._last_terminal_response: tuple[str, int] | None = None
        self._active_utterance_id: str | None = None
        self._next_transcript_sequence = 0
        self._last_finalized_utterance: str | None = None

    @property
    def opened(self) -> bool:
        return self._opened

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def closed(self) -> bool:
        return self._state.closed

    @property
    def active_playback(self) -> PlaybackRef | None:
        return self._active_response

    @property
    def selected_media_profile(self) -> str | None:
        return self._selected_media_profile

    @property
    def uplink_generation(self) -> int:
        return max(1, self._state.playback_generation)

    async def receive_control(self, raw: str) -> str | None:
        message = decode_control(raw)
        message_type = message.get("type")
        if message_type == "session.open":
            return self._open(message)
        self._ensure_open()
        if message_type == "response.cancel":
            return await self._cancel(message)
        if message_type == "session.close":
            return await self._receive_close(message)
        raise RvaBindingError("unknown_client_message")

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

    def serialize_audio(self, payload: bytes, *, timestamp: int, target: PlaybackRef) -> bytes:
        self._ensure_open()
        if self._selected_media_profile != WSS_PROFILE:
            raise RvaBindingError("transport_mismatch")
        self._require_active_response(self._active_response_id or "", target)
        if not self._state.accepts_callback(target):
            raise RvaBindingError("stale_generation")
        if self._next_downlink_sequence > 0xFFFFFFFF:
            raise RvaBindingError("media_sequence_exhausted")
        frame = WssMediaFrame(
            media_id=self._media_id,
            media_epoch=self._media_epoch,
            sequence=self._next_downlink_sequence,
            timestamp=require_uint32(timestamp, "timestamp"),
            generation=target.generation,
            payload=payload,
        )
        encoded = frame.serialize()
        self._next_downlink_sequence += 1
        return encoded

    def transcript_delta(self, *, utterance_id: str, sequence: int, text: str) -> str:
        self._ensure_open()
        utterance_id = require_identifier(utterance_id, "utterance_id")
        sequence = require_uint32(sequence, "sequence")
        self._require_text(text, maximum_bytes=4_096, allow_empty=False)
        self._advance_transcript(utterance_id, sequence, final=False)
        return self._event(
            "transcript.delta",
            utterance_id=utterance_id,
            sequence=sequence,
            text=text,
        )

    def transcript_final(self, *, utterance_id: str, sequence: int, text: str) -> str:
        self._ensure_open()
        utterance_id = require_identifier(utterance_id, "utterance_id")
        sequence = require_uint32(sequence, "sequence")
        self._require_text(text, maximum_bytes=16_384, allow_empty=True)
        self._advance_transcript(utterance_id, sequence, final=True)
        return self._event(
            "transcript.final",
            utterance_id=utterance_id,
            sequence=sequence,
            text=text,
        )

    async def response_begin(self, *, response_id: str) -> tuple[PlaybackRef, str]:
        self._ensure_open()
        response_id = require_identifier(response_id, "response_id")
        if self._active_response is not None:
            raise RvaBindingError("response_already_active")
        if self._last_terminal_response is not None and response_id == self._last_terminal_response[0]:
            raise RvaBindingError("response_already_terminal")
        target = await self._state.begin_playback()
        self._active_response_id = response_id
        self._active_response = target
        self._next_response_text_sequence = 0
        return target, self._event("response.begin", response_id=response_id, generation=target.generation)

    def response_text(self, *, response_id: str, target: PlaybackRef, sequence: int, text: str) -> str:
        self._ensure_open()
        self._require_active_response(response_id, target)
        sequence = require_uint32(sequence, "sequence")
        if sequence != self._next_response_text_sequence:
            raise RvaBindingError("invalid_sequence")
        self._require_text(text, maximum_bytes=4_096, allow_empty=False)
        self._next_response_text_sequence += 1
        return self._event(
            "response.text",
            response_id=response_id,
            generation=target.generation,
            sequence=sequence,
            text=text,
        )

    async def response_end(self, *, response_id: str, target: PlaybackRef, failed: bool = False) -> str:
        self._ensure_open()
        self._require_active_response(response_id, target)
        if not await self._state.finish_playback(target):
            raise RvaBindingError("stale_generation")
        self._mark_response_terminal(response_id, target)
        return self._event(
            "response.end",
            response_id=response_id,
            generation=target.generation,
            reason="failed" if failed else "completed",
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
        await self._state.close()

    def _open(self, message: dict[str, object]) -> str:
        if self._opened:
            raise RvaBindingError("duplicate_session_open")
        if self._state.closed:
            raise RvaBindingError("session_closed")
        opened = parse_session_open_object(message)
        if opened.device_id != self._expected_device_id:
            raise RvaBindingError("device_id_mismatch")
        offered = tuple(profile for profile in opened.supported_media_profiles if profile in self._allowed_profiles)
        if not offered:
            raise RvaBindingError("unsupported_media_profile")
        if opened.preferred_media_profile in offered:
            selected = opened.preferred_media_profile
        elif WSS_PROFILE in offered:
            selected = WSS_PROFILE
        else:
            selected = offered[0]
        if selected == UDP_PROFILE and self._udp_grant is None:
            raise RvaBindingError("udp_unavailable")
        self._opened = True
        self._client_request_id = opened.request_id
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

    async def _cancel(self, message: dict[str, object]) -> str:
        require_exact_fields(message, {"type", "session_id", "session_epoch", "target", "reason"})
        self._require_context(message)
        reason = message["reason"]
        if not isinstance(reason, str) or reason not in {"user_request", "barge_in", "new_wake", "session_close"}:
            raise RvaBindingError("invalid_cancel_reason")
        raw_target = message["target"]
        if not isinstance(raw_target, dict):
            raise RvaBindingError("invalid_cancel_target")
        require_exact_fields(raw_target, {"response_id", "generation"})
        response_id = require_identifier(raw_target["response_id"], "response_id")
        generation = require_uint32(raw_target["generation"], "generation", minimum=1)
        target = PlaybackRef(self._session_epoch, generation)
        self._require_active_response(response_id, target)
        disposition = await self._state.cancel_playback(target)
        if disposition is not CancelDisposition.APPLIED:
            raise RvaBindingError("stale_cancel_target")
        self._mark_response_terminal(response_id, target)
        return self._event(
            "response.cancelled",
            target={"response_id": response_id, "generation": generation},
            reason="cancelled",
        )

    async def _receive_close(self, message: dict[str, object]) -> str:
        required = {"type", "session_id", "session_epoch", "reason", "initiated_by"}
        actual = frozenset(message)
        if actual not in {frozenset(required), frozenset(required | {"detail"})}:
            require_exact_fields(message, required)
        self._require_context(message)
        initiated_by = message["initiated_by"]
        if not isinstance(initiated_by, str) or initiated_by != "device":
            raise RvaBindingError("invalid_close_initiator")
        reason = message["reason"]
        if not isinstance(reason, str) or reason not in {"normal", "idle_timeout", "network_change", "protocol_error"}:
            raise RvaBindingError("invalid_close_reason")
        if "detail" in message:
            self._require_text(message["detail"], maximum_bytes=256, allow_empty=True)
        response = self._event(
            "session.close",
            reason=reason,
            initiated_by="server",
        )
        await self._state.close()
        return response

    def _require_context(self, message: dict[str, object]) -> None:
        if message.get("session_id") != self._session_id or message.get("session_epoch") != self._session_epoch:
            raise RvaBindingError("stale_session")

    def _require_active_response(self, response_id: str, target: PlaybackRef) -> None:
        if response_id != self._active_response_id or target != self._active_response:
            raise RvaBindingError("stale_generation")

    def _mark_response_terminal(self, response_id: str, target: PlaybackRef) -> None:
        self._last_terminal_response = (response_id, target.generation)
        self._active_response_id = None
        self._active_response = None

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
            {
                "type": event_type,
                "session_id": self._session_id,
                "session_epoch": self._session_epoch,
                **fields,
            }
        )

    def _ensure_open(self) -> None:
        if self._state.closed:
            raise RvaBindingError("session_closed")
        if not self._opened:
            raise RvaBindingError("session_not_open")

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
    "InboundAudioPacket",
    "MEDIA_MAX_PAYLOAD_BYTES",
    "RvaWssBinding",
]
