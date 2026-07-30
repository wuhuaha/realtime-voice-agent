from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from rva_desktop.config import ClientConfig, MediaProfile
from rva_desktop.errors import ProtocolError
from rva_desktop.protocol import (
    FLAG_AUDIO,
    MediaFrame,
    PlaybackTarget,
    ReplayWindow,
    SessionOpened,
    UdpCipher,
    build_session_open,
    parse_session_opened,
)
from rva_desktop.session import SessionState

ROOT = Path(__file__).resolve().parents[3]


def test_canonical_control_schema_and_fixtures_are_consumed_without_copying() -> None:
    schema = json.loads((ROOT / "protocol/rva_control_v2/messages.schema.json").read_text(encoding="utf-8"))
    fixtures = json.loads(
        (ROOT / "protocol/rva_control_v2/fixtures/positive.json").read_text(encoding="utf-8")
    )["vectors"]
    assert schema["$id"].endswith("/rva-control-v2/messages.schema.json")
    assert {item["id"] for item in fixtures} >= {"open-with-v2-profiles", "opened-wss-v3", "opened-udp-v2"}

    opened = next(item["message"] for item in fixtures if item["id"] == "opened-udp-v2")
    parsed = parse_session_opened(opened, request_id="open-002")
    assert parsed.media_id.hex() == "fedcba9876543210"
    assert parsed.udp_grant is not None
    assert len(parsed.udp_grant.downlink_key) == 16


def test_all_canonical_control_schema_vectors_remain_authoritative() -> None:
    schema = json.loads((ROOT / "protocol/rva_control_v2/messages.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    positive = json.loads(
        (ROOT / "protocol/rva_control_v2/fixtures/positive.json").read_text(encoding="utf-8")
    )["vectors"]
    negative = json.loads(
        (ROOT / "protocol/rva_control_v2/fixtures/negative.json").read_text(encoding="utf-8")
    )["schema_vectors"]

    for vector in positive:
        assert not list(validator.iter_errors(vector["message"])), vector["id"]
    for vector in negative:
        assert list(validator.iter_errors(vector["message"])), vector["id"]


def test_generated_outbound_control_messages_validate_against_canonical_schema() -> None:
    schema = json.loads((ROOT / "protocol/rva_control_v2/messages.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    config = ClientConfig(
        director_url="https://director.test",
        bootstrap_token="validator-bootstrap-token",
        device_id="desktop-1",
        supported_profiles=(MediaProfile.WSS_OPUS_V3,),
        preferred_profile=MediaProfile.WSS_OPUS_V3,
    )
    opened = SessionOpened(
        request_id="open-1",
        session_id="session-1",
        session_epoch="epoch-1",
        media_id=bytes.fromhex("0123456789abcdef"),
        media_epoch=7,
        selected_profile=MediaProfile.WSS_OPUS_V3,
        heartbeat_interval_ms=15_000,
        idle_timeout_ms=45_000,
        udp_grant=None,
    )
    state = SessionState(opened)
    target = PlaybackTarget("response-1", 1)
    identity = {"session_id": "session-1", "session_epoch": "epoch-1"}
    state.accept_control({"type": "response.begin", **identity, "response_id": "response-1", "generation": 1})
    state.accept_media(MediaFrame(FLAG_AUDIO, opened.media_id, 7, 0, 0, 1, b"opus"))
    started = state.playback_started(target, 0)
    state.accept_control(
        {
            "type": "response.end",
            **identity,
            "response_id": "response-1",
            "generation": 1,
            "outcome": "completed",
            "final_media_sequence": 0,
        }
    )
    ended = state.playback_ended(
        target,
        outcome="completed",
        played_samples=960,
        last_media_sequence=0,
    )
    close = state.close_message("normal", detail="contract test complete")

    messages = [build_session_open(config, "open-1"), started, ended, close]
    for message in messages:
        assert not list(validator.iter_errors(message)), message["type"]


def test_udp_codec_matches_every_canonical_positive_vector() -> None:
    vectors = json.loads(
        (ROOT / "protocol/udp_opus_gcm_v2/fixtures/positive.json").read_text(encoding="utf-8")
    )["vectors"]
    for vector in vectors:
        fields = vector["fields"]
        frame = MediaFrame(
            flags=fields["flags"],
            media_id=bytes.fromhex(fields["media_id_hex"]),
            media_epoch=fields["media_epoch"],
            sequence=fields["sequence"],
            timestamp=fields["timestamp"],
            generation=fields["generation"],
            payload=bytes.fromhex(vector["payload_hex"]),
        )
        cipher = UdpCipher(bytes.fromhex(vector["key_hex"]), bytes.fromhex(vector["salt_hex"]))
        wire = cipher.encrypt(frame)
        assert wire.hex() == vector["datagram_hex"]
        assert cipher.decrypt(wire) == frame


def test_udp_authentication_failure_is_rejected() -> None:
    cipher = UdpCipher(bytes(16), bytes(8))
    frame = MediaFrame(1, bytes(8), 1, 0, 0, 0, b"opus")
    wire = bytearray(cipher.encrypt(frame))
    wire[-1] ^= 1
    with pytest.raises(ProtocolError, match="udp_authentication_failed"):
        cipher.decrypt(bytes(wire))


def test_replay_window_does_not_advance_until_committed() -> None:
    replay = ReplayWindow()
    assert replay.acceptable(7)
    assert replay.acceptable(7)
    replay.commit(7)
    assert not replay.acceptable(7)
    assert not replay.acceptable(7 + 1025)


def test_media_header_rejects_previous_wire_version() -> None:
    frame = bytearray(MediaFrame(1, bytes(8), 1, 0, 0, 0, b"opus").encode_plain())
    frame[2] = 1
    with pytest.raises(ProtocolError, match="unsupported_media_header"):
        MediaFrame.decode_plain(bytes(frame))
