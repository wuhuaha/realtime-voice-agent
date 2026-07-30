from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..config import ClientConfig, MediaProfile
from ..errors import ProtocolError

CONTROL_MAX_BYTES = 32_768
AUDIO_PROFILE = {"codec": "opus", "sample_rate_hz": 16_000, "channels": 1, "frame_duration_ms": 60}
_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SERVER_TYPES = {
    "session.opened",
    "transcript.delta",
    "transcript.final",
    "response.begin",
    "response.text",
    "response.end",
    "playback.stop",
    "session.error",
    "session.close",
}
_SESSION_CLOSE_REASONS = {"normal", "idle_timeout", "network_change", "protocol_error", "server_shutdown"}


@dataclass(frozen=True, slots=True)
class UdpGrant:
    host: str
    port: int
    expires_at_ms: int
    refresh_after_ms: int
    uplink_key: bytes = field(repr=False)
    uplink_salt: bytes = field(repr=False)
    downlink_key: bytes = field(repr=False)
    downlink_salt: bytes = field(repr=False)
    probe_timeout_ms: int


@dataclass(frozen=True, slots=True)
class SessionOpened:
    request_id: str
    session_id: str
    session_epoch: str
    media_id: bytes
    media_epoch: int
    selected_profile: MediaProfile
    heartbeat_interval_ms: int
    idle_timeout_ms: int
    udp_grant: UdpGrant | None


def build_session_open(config: ClientConfig, request_id: str) -> dict[str, Any]:
    _identifier(request_id, "request_id")
    return {
        "type": "session.open",
        "protocol_version": 2,
        "request_id": request_id,
        "device_id": config.device_id,
        "supported_media_profiles": [profile.value for profile in config.supported_profiles],
        "preferred_media_profile": config.preferred_profile.value,
        "audio": dict(AUDIO_PROFILE),
        "capabilities": config.capabilities.as_wire(),
    }


def encode_control(message: dict[str, Any]) -> str:
    wire = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    if len(wire.encode("utf-8")) > CONTROL_MAX_BYTES:
        raise ProtocolError("message_too_large")
    return wire


def decode_control(wire: str) -> dict[str, Any]:
    if not isinstance(wire, str):
        raise ProtocolError("control_frame_must_be_text")
    if len(wire.encode("utf-8")) > CONTROL_MAX_BYTES:
        raise ProtocolError("message_too_large")
    try:
        value = json.loads(wire, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ProtocolError("invalid_json") from exc
    if not isinstance(value, dict):
        raise ProtocolError("control_message_must_be_object")
    return value


def parse_session_opened(message: dict[str, Any], *, request_id: str) -> SessionOpened:
    validate_server_message(message)
    if message["type"] != "session.opened":
        raise ProtocolError("expected_session_opened")
    if message["request_id"] != request_id:
        raise ProtocolError("request_id_mismatch")
    selected = MediaProfile(message["selected_media_profile"])
    grant = _parse_udp_grant(message.get("udp_grant"))
    if (selected is MediaProfile.UDP_OPUS_GCM_V2) != (grant is not None):
        raise ProtocolError("invalid_udp_grant")
    return SessionOpened(
        request_id=request_id,
        session_id=_identifier(message["session_id"], "session_id"),
        session_epoch=_identifier(message["session_epoch"], "session_epoch"),
        media_id=_media_id(message["media_id"]),
        media_epoch=_uint32(message["media_epoch"], "media_epoch", minimum=1),
        selected_profile=selected,
        heartbeat_interval_ms=_bounded_int(message["heartbeat_interval_ms"], 5_000, 60_000, "heartbeat"),
        idle_timeout_ms=_bounded_int(message["idle_timeout_ms"], 15_000, 180_000, "idle_timeout"),
        udp_grant=grant,
    )


def validate_server_message(message: dict[str, Any]) -> None:
    message_type = message.get("type")
    if message_type not in _SERVER_TYPES:
        raise ProtocolError("unknown_server_message")
    if message_type == "session.opened":
        required = {
            "type", "request_id", "session_id", "session_epoch", "media_id", "media_epoch",
            "selected_media_profile", "audio", "heartbeat_interval_ms", "idle_timeout_ms",
            "max_control_message_bytes",
        }
        if message.get("selected_media_profile") == MediaProfile.UDP_OPUS_GCM_V2.value:
            required.add("udp_grant")
        _exact(message, required)
        if message["audio"] != AUDIO_PROFILE or message["max_control_message_bytes"] != CONTROL_MAX_BYTES:
            raise ProtocolError("unsupported_session_parameters")
        _identifier(message["request_id"], "request_id")
        return
    required = {"type", "session_id", "session_epoch"}
    _identifier(message.get("session_id"), "session_id")
    _identifier(message.get("session_epoch"), "session_epoch")
    if message_type.startswith("transcript."):
        required |= {"utterance_id", "sequence", "text"}
    elif message_type == "response.begin":
        required |= {"response_id", "generation"}
    elif message_type == "response.text":
        required |= {"response_id", "generation", "sequence", "text"}
    elif message_type == "response.end":
        required |= {"response_id", "generation", "outcome"}
        if message.get("outcome") == "completed":
            required.add("final_media_sequence")
        elif message.get("outcome") == "failed":
            required.add("error_code")
    elif message_type == "playback.stop":
        required |= {"target", "fence_generation", "cause"}
    elif message_type == "session.error":
        required |= {"code", "retryable", "message"}
    elif message_type == "session.close":
        required |= {"reason", "initiated_by"}
    _exact(message, required, optional={"detail"} if message_type == "session.close" else set())
    if message_type.startswith("transcript."):
        _identifier(message["utterance_id"], "utterance_id")
        _uint32(message["sequence"], "sequence")
        _text(
            message["text"],
            4_096 if message_type.endswith("delta") else 16_384,
            allow_empty=message_type.endswith("final"),
        )
    elif message_type == "response.begin":
        _identifier(message["response_id"], "response_id")
        _uint32(message["generation"], "generation", minimum=1)
    elif message_type == "response.text":
        _identifier(message["response_id"], "response_id")
        _uint32(message["generation"], "generation", minimum=1)
        _uint32(message["sequence"], "sequence")
        _text(message["text"], 4_096, allow_empty=False)
    elif message_type == "response.end":
        _identifier(message["response_id"], "response_id")
        _uint32(message["generation"], "generation", minimum=1)
        if message["outcome"] not in {"completed", "cancelled", "failed"}:
            raise ProtocolError("invalid_response_outcome")
        if "final_media_sequence" in message:
            _uint32(message["final_media_sequence"], "final_media_sequence")
        if "error_code" in message:
            _error_code(message["error_code"])
    elif message_type == "playback.stop":
        _target(message["target"])
        _uint32(message["fence_generation"], "fence_generation", minimum=1)
        if message["cause"] not in {
            "explicit_user_request", "recognized_interrupt", "session_close", "response_failed"
        }:
            raise ProtocolError("invalid_stop_cause")
    elif message_type == "session.error":
        _error_code(message["code"])
        if type(message["retryable"]) is not bool:
            raise ProtocolError("invalid_retryable")
        _text(message["message"], 512, allow_empty=True)
    elif message_type == "session.close":
        if message["reason"] not in _SESSION_CLOSE_REASONS:
            raise ProtocolError("invalid_close_reason")
        if message["initiated_by"] not in {"server", "device"}:
            raise ProtocolError("invalid_close_initiator")
        if "detail" in message:
            _text(message["detail"], 256, allow_empty=True)


def _parse_udp_grant(value: object) -> UdpGrant | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ProtocolError("invalid_udp_grant")
    fields = {
        "host", "port", "expires_at_ms", "refresh_after_ms", "uplink_key_b64", "uplink_salt_b64",
        "downlink_key_b64", "downlink_salt_b64", "probe_timeout_ms",
    }
    _exact(value, fields)
    try:
        uplink_key = base64.b64decode(value["uplink_key_b64"], validate=True)
        uplink_salt = base64.b64decode(value["uplink_salt_b64"], validate=True)
        downlink_key = base64.b64decode(value["downlink_key_b64"], validate=True)
        downlink_salt = base64.b64decode(value["downlink_salt_b64"], validate=True)
    except (ValueError, TypeError) as exc:
        raise ProtocolError("invalid_udp_grant") from exc
    if any((len(uplink_key) != 16, len(downlink_key) != 16, len(uplink_salt) != 8, len(downlink_salt) != 8)):
        raise ProtocolError("invalid_udp_grant")
    host = value["host"]
    if not isinstance(host, str) or not host or len(host) > 253:
        raise ProtocolError("invalid_udp_grant")
    return UdpGrant(
        host=host,
        port=_bounded_int(value["port"], 1, 65_535, "udp_port"),
        expires_at_ms=_bounded_int(value["expires_at_ms"], 1, 2**63 - 1, "udp_expiry"),
        refresh_after_ms=_bounded_int(value["refresh_after_ms"], 1_000, 3_600_000, "udp_refresh"),
        uplink_key=uplink_key,
        uplink_salt=uplink_salt,
        downlink_key=downlink_key,
        downlink_salt=downlink_salt,
        probe_timeout_ms=_bounded_int(value["probe_timeout_ms"], 100, 10_000, "udp_probe_timeout"),
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("duplicate_json_key")
        result[key] = value
    return result


def _exact(message: dict[str, Any], expected: set[str], *, optional: set[str] | None = None) -> None:
    actual = set(message)
    allowed = expected | (optional or set())
    if expected - actual:
        raise ProtocolError("missing_field")
    if actual - allowed:
        raise ProtocolError("unknown_field")


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ProtocolError(f"invalid_{field}")
    return value


def _media_id(value: object) -> bytes:
    if not isinstance(value, str):
        raise ProtocolError("invalid_media_id")
    try:
        result = bytes.fromhex(value)
    except ValueError as exc:
        raise ProtocolError("invalid_media_id") from exc
    if len(result) != 8 or value.lower() != value:
        raise ProtocolError("invalid_media_id")
    return result


def _bounded_int(value: object, minimum: int, maximum: int, field: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProtocolError(f"invalid_{field}")
    return value


def _uint32(value: object, field: str, *, minimum: int = 0) -> int:
    return _bounded_int(value, minimum, 0xFFFFFFFF, field)


def _target(value: object) -> None:
    if not isinstance(value, dict):
        raise ProtocolError("invalid_playback_target")
    _exact(value, {"response_id", "generation"})
    _identifier(value["response_id"], "response_id")
    _uint32(value["generation"], "generation", minimum=1)


def _error_code(value: object) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[a-z0-9_]{1,64}", value) is None:
        raise ProtocolError("invalid_error_code")


def _text(value: object, maximum_bytes: int, *, allow_empty: bool) -> None:
    if not isinstance(value, str) or (not allow_empty and not value) or "\x00" in value:
        raise ProtocolError("invalid_text")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise ProtocolError("text_too_large")
