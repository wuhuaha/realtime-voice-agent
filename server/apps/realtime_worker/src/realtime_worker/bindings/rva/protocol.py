"""Strict wire primitives for rva-control-v1 and wss-opus-v2."""

from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass
from typing import Any

CONTROL_VERSION = 1
CONTROL_MAX_BYTES = 32_768
WSS_PROFILE = "wss-opus-v2"
UDP_PROFILE = "udp-opus-gcm-v1"
SUPPORTED_PROFILES = frozenset({WSS_PROFILE, UDP_PROFILE})

# Keep the canonical udp-opus-gcm-v1 wire identity. WSS v2 deliberately
# reuses the same typed header so a profile has one byte contract.
MEDIA_MAGIC = b"VA"
MEDIA_WIRE_VERSION = 1
MEDIA_FLAG_AUDIO = 1
MEDIA_HEADER_BYTES = 32
MEDIA_MAX_PAYLOAD_BYTES = 1_200
MEDIA_MAX_FRAME_BYTES = MEDIA_HEADER_BYTES + MEDIA_MAX_PAYLOAD_BYTES
_MEDIA_HEADER = struct.Struct(">2sBB8sIIIII")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class RvaBindingError(ValueError):
    """A stable binding failure that a WebSocket adapter can map to policy close."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(detail or code)
        self.code = code


class RvaMessageTooLarge(RvaBindingError):
    def __init__(self, detail: str = "control message exceeds profile limit") -> None:
        super().__init__("message_too_large", detail)


@dataclass(frozen=True, slots=True)
class SessionOpen:
    request_id: str
    device_id: str
    supported_media_profiles: tuple[str, ...]
    preferred_media_profile: str
    capabilities: dict[str, bool]


@dataclass(frozen=True, slots=True)
class WssMediaFrame:
    media_id: bytes
    media_epoch: int
    sequence: int
    timestamp: int
    generation: int
    payload: bytes

    def serialize(self, *, flags: int = MEDIA_FLAG_AUDIO) -> bytes:
        _validate_uint32("media_epoch", self.media_epoch, minimum=1)
        _validate_uint32("sequence", self.sequence)
        _validate_uint32("timestamp", self.timestamp)
        _validate_uint32("generation", self.generation)
        if not isinstance(self.media_id, bytes) or len(self.media_id) != 8:
            raise RvaBindingError("invalid_media_id")
        if flags != MEDIA_FLAG_AUDIO:
            raise RvaBindingError("invalid_media_flags")
        if not isinstance(self.payload, bytes) or not self.payload or len(self.payload) > MEDIA_MAX_PAYLOAD_BYTES:
            raise RvaBindingError("invalid_media_payload")
        header = _MEDIA_HEADER.pack(
            MEDIA_MAGIC,
            MEDIA_WIRE_VERSION,
            flags,
            self.media_id,
            self.media_epoch,
            self.sequence,
            self.timestamp,
            self.generation,
            len(self.payload),
        )
        return header + self.payload

    @classmethod
    def parse(cls, frame: bytes) -> WssMediaFrame:
        if not isinstance(frame, bytes):
            raise RvaBindingError("media_frame_must_be_binary")
        if len(frame) > MEDIA_MAX_FRAME_BYTES:
            raise RvaBindingError("media_frame_too_large")
        if len(frame) < MEDIA_HEADER_BYTES:
            raise RvaBindingError("truncated_media_header")
        magic, version, flags, media_id, media_epoch, sequence, timestamp, generation, payload_length = (
            _MEDIA_HEADER.unpack_from(frame)
        )
        if magic != MEDIA_MAGIC or version != MEDIA_WIRE_VERSION:
            raise RvaBindingError("unsupported_media_header")
        if flags != MEDIA_FLAG_AUDIO:
            raise RvaBindingError("invalid_media_flags")
        payload = frame[MEDIA_HEADER_BYTES:]
        if payload_length != len(payload):
            raise RvaBindingError("media_length_mismatch")
        if media_epoch == 0:
            raise RvaBindingError("invalid_media_epoch")
        if not payload or payload_length > MEDIA_MAX_PAYLOAD_BYTES:
            raise RvaBindingError("invalid_media_payload")
        return cls(media_id, media_epoch, sequence, timestamp, generation, payload)


def decode_control(raw: str) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise RvaBindingError("control_frame_must_be_text")
    try:
        encoded = raw.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise RvaBindingError("invalid_utf8") from exc
    if len(encoded) > CONTROL_MAX_BYTES:
        raise RvaMessageTooLarge()
    try:
        message = json.loads(raw, object_pairs_hook=_unique_object)
    except RvaBindingError:
        raise
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise RvaBindingError("invalid_json") from exc
    if not isinstance(message, dict):
        raise RvaBindingError("control_message_must_be_object")
    return message


def encode_control(message: dict[str, object]) -> str:
    encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    try:
        encoded_bytes = encoded.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise RvaBindingError("invalid_utf8") from exc
    if len(encoded_bytes) > CONTROL_MAX_BYTES:
        raise RvaMessageTooLarge("encoded control event exceeds profile limit")
    return encoded


def parse_session_open(raw: str) -> SessionOpen:
    return parse_session_open_object(decode_control(raw))


def parse_session_open_object(message: dict[str, Any]) -> SessionOpen:
    required = {
        "type",
        "protocol_version",
        "request_id",
        "device_id",
        "supported_media_profiles",
        "preferred_media_profile",
        "audio",
        "capabilities",
    }
    _require_exact_fields(message, required)
    if message["type"] != "session.open":
        raise RvaBindingError("expected_session_open")
    if type(message["protocol_version"]) is not int or message["protocol_version"] != CONTROL_VERSION:
        raise RvaBindingError("unsupported_version")
    request_id = require_identifier(message["request_id"], "request_id")
    device_id = require_identifier(message["device_id"], "device_id")
    profiles = message["supported_media_profiles"]
    if not isinstance(profiles, list) or not 1 <= len(profiles) <= 2:
        raise RvaBindingError("invalid_media_profiles")
    if any(not isinstance(profile, str) or profile not in SUPPORTED_PROFILES for profile in profiles):
        raise RvaBindingError("unsupported_media_profile")
    if len(set(profiles)) != len(profiles):
        raise RvaBindingError("duplicate_media_profile")
    preferred = message["preferred_media_profile"]
    if not isinstance(preferred, str) or preferred not in profiles:
        raise RvaBindingError("unsupported_preference")
    _validate_audio(message["audio"])
    capabilities = _validate_capabilities(message["capabilities"])
    return SessionOpen(request_id, device_id, tuple(profiles), preferred, capabilities)


def require_identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise RvaBindingError(f"invalid_{field}")
    return value


def require_uint32(value: object, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int:
        raise RvaBindingError(f"invalid_{field}")
    _validate_uint32(field, value, minimum=minimum)
    return value


def require_exact_fields(message: dict[str, Any], fields: set[str]) -> None:
    _require_exact_fields(message, fields)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RvaBindingError("duplicate_json_key", f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _require_exact_fields(message: dict[str, Any], fields: set[str]) -> None:
    if set(message) != fields:
        missing = fields - set(message)
        raise RvaBindingError("missing_field" if missing else "unknown_field")


def _validate_audio(value: object) -> None:
    expected = {"codec": "opus", "sample_rate_hz": 16_000, "channels": 1, "frame_duration_ms": 60}
    if not isinstance(value, dict) or value != expected:
        raise RvaBindingError("unsupported_audio_profile")


def _validate_capabilities(value: object) -> dict[str, bool]:
    allowed = {"aec", "vad", "wake_word", "display", "touch"}
    if not isinstance(value, dict) or any(key not in allowed for key in value):
        raise RvaBindingError("invalid_capabilities")
    if any(type(capability) is not bool for capability in value.values()):
        raise RvaBindingError("invalid_capabilities")
    return dict(value)


def _validate_uint32(field: str, value: int, *, minimum: int = 0) -> None:
    if type(value) is not int or not minimum <= value <= 0xFFFFFFFF:
        raise RvaBindingError(f"invalid_{field}")
