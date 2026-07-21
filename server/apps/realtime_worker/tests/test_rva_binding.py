from __future__ import annotations

import json

import pytest
from realtime_worker.bindings.rva import (
    CONTROL_MAX_BYTES,
    MEDIA_HEADER_BYTES,
    MEDIA_MAX_FRAME_BYTES,
    MEDIA_MAX_PAYLOAD_BYTES,
    InboundAudioPacket,
    RvaBindingError,
    RvaMessageTooLarge,
    RvaWssBinding,
    WssMediaFrame,
    parse_session_open,
)
from realtime_worker.voice.session import PlaybackRef


class FakeAudioPort:
    def __init__(self) -> None:
        self.packets: list[InboundAudioPacket] = []
        self.close_calls = 0

    async def receive_audio(self, packet: InboundAudioPacket) -> None:
        self.packets.append(packet)

    async def close(self) -> None:
        self.close_calls += 1


class FakeAgentPort:
    def __init__(self) -> None:
        self.interrupts: list[PlaybackRef] = []
        self.close_calls = 0

    async def interrupt(self, target: PlaybackRef) -> None:
        self.interrupts.append(target)

    async def close(self) -> None:
        self.close_calls += 1


def session_open(*, profiles: list[str] | None = None, preferred: str = "wss-opus-v2") -> str:
    profiles = profiles or ["wss-opus-v2", "udp-opus-gcm-v1"]
    return json.dumps(
        {
            "type": "session.open",
            "protocol_version": 1,
            "request_id": "open-001",
            "device_id": "device-001",
            "supported_media_profiles": profiles,
            "preferred_media_profile": preferred,
            "audio": {"codec": "opus", "sample_rate_hz": 16_000, "channels": 1, "frame_duration_ms": 60},
            "capabilities": {"aec": True, "vad": True, "wake_word": True, "display": True, "touch": True},
        }
    )


def create_binding() -> tuple[RvaWssBinding, FakeAudioPort, FakeAgentPort]:
    audio = FakeAudioPort()
    agent = FakeAgentPort()
    binding = RvaWssBinding(
        expected_device_id="device-001",
        session_id="session-001",
        session_epoch="grant-epoch-001",
        media_id=bytes.fromhex("0123456789abcdef"),
        media_epoch=7,
        audio_port=audio,
        agent_port=agent,
    )
    return binding, audio, agent


async def open_binding(binding: RvaWssBinding) -> dict[str, object]:
    response = await binding.receive_control(session_open(preferred="udp-opus-gcm-v1"))
    assert response is not None
    return json.loads(response)


@pytest.mark.contract
def test_session_open_parser_rejects_duplicate_unknown_and_oversized_input() -> None:
    opened = parse_session_open(session_open())
    assert opened.device_id == "device-001"
    assert opened.supported_media_profiles == ("wss-opus-v2", "udp-opus-gcm-v1")

    with pytest.raises(RvaBindingError, match="duplicate") as duplicate:
        parse_session_open('{"type":"session.open","type":"session.open"}')
    assert duplicate.value.code == "duplicate_json_key"

    unknown = json.loads(session_open())
    unknown["mode"] = "realtime"
    with pytest.raises(RvaBindingError) as extra:
        parse_session_open(json.dumps(unknown))
    assert extra.value.code == "unknown_field"

    with pytest.raises(RvaMessageTooLarge) as oversized:
        parse_session_open(" " * (CONTROL_MAX_BYTES + 1))
    assert oversized.value.code == "message_too_large"


@pytest.mark.contract
async def test_handshake_selects_one_wss_profile_and_returns_media_identity() -> None:
    binding, _, _ = create_binding()

    response = await open_binding(binding)

    assert response == {
        "type": "session.opened",
        "request_id": "open-001",
        "session_id": "session-001",
        "session_epoch": "grant-epoch-001",
        "media_id": "0123456789abcdef",
        "media_epoch": 7,
        "selected_media_profile": "wss-opus-v2",
        "audio": {"codec": "opus", "sample_rate_hz": 16_000, "channels": 1, "frame_duration_ms": 60},
        "heartbeat_interval_ms": 15_000,
        "idle_timeout_ms": 45_000,
        "max_control_message_bytes": CONTROL_MAX_BYTES,
    }
    assert binding.opened is True

    with pytest.raises(RvaBindingError) as repeated:
        await binding.receive_control(session_open())
    assert repeated.value.code == "duplicate_session_open"


@pytest.mark.contract
async def test_handshake_selects_udp_and_emits_schema_shaped_grant() -> None:
    audio = FakeAudioPort()
    agent = FakeAgentPort()
    udp_grant = {
        "host": "voice.example.test",
        "port": 8443,
        "expires_at_ms": 1_780_000_000_000,
        "uplink_key_b64": "AAAAAAAAAAAAAAAAAAAAAA==",
        "uplink_salt_b64": "AAAAAAAAAAA=",
        "downlink_key_b64": "/////////////////////w==",
        "downlink_salt_b64": "//////////8=",
        "probe_timeout_ms": 1_500,
    }
    binding = RvaWssBinding(
        expected_device_id="device-001",
        session_id="session-001",
        session_epoch="grant-epoch-001",
        media_id=bytes.fromhex("fedcba9876543210"),
        media_epoch=8,
        allowed_profiles=frozenset({"wss-opus-v2", "udp-opus-gcm-v1"}),
        udp_grant=udp_grant,
        audio_port=audio,
        agent_port=agent,
    )

    raw = await binding.receive_control(session_open(preferred="udp-opus-gcm-v1"))
    assert raw is not None
    opened = json.loads(raw)
    assert opened["selected_media_profile"] == "udp-opus-gcm-v1"
    assert opened["media_id"] == "fedcba9876543210"
    assert opened["media_epoch"] == 8
    assert opened["udp_grant"] == udp_grant
    assert binding.selected_media_profile == "udp-opus-gcm-v1"
    with pytest.raises(RvaBindingError) as wrong_transport:
        await binding.receive_media(b"not-wss-media")
    assert wrong_transport.value.code == "transport_mismatch"


@pytest.mark.contract
async def test_udp_selection_fails_closed_without_session_grant() -> None:
    audio = FakeAudioPort()
    agent = FakeAgentPort()
    binding = RvaWssBinding(
        expected_device_id="device-001",
        session_id="session-001",
        session_epoch="grant-epoch-001",
        media_id=bytes.fromhex("0123456789abcdef"),
        media_epoch=7,
        allowed_profiles=frozenset({"udp-opus-gcm-v1"}),
        audio_port=audio,
        agent_port=agent,
    )
    with pytest.raises(RvaBindingError) as unavailable:
        await binding.receive_control(
            session_open(profiles=["udp-opus-gcm-v1"], preferred="udp-opus-gcm-v1")
        )
    assert unavailable.value.code == "udp_unavailable"


@pytest.mark.contract
async def test_wss_binding_rejects_unoffered_wss_and_device_mismatch() -> None:
    binding, _, _ = create_binding()
    with pytest.raises(RvaBindingError) as unsupported:
        await binding.receive_control(session_open(profiles=["udp-opus-gcm-v1"], preferred="udp-opus-gcm-v1"))
    assert unsupported.value.code == "unsupported_media_profile"

    binding, _, _ = create_binding()
    mismatched = json.loads(session_open())
    mismatched["device_id"] = "device-other"
    with pytest.raises(RvaBindingError) as wrong_device:
        await binding.receive_control(json.dumps(mismatched))
    assert wrong_device.value.code == "device_id_mismatch"


@pytest.mark.contract
def test_wss_media_header_roundtrip_and_bounds() -> None:
    frame = WssMediaFrame(
        media_id=bytes.fromhex("0123456789abcdef"),
        media_epoch=7,
        sequence=3,
        timestamp=2_880,
        generation=4,
        payload=b"opus",
    )
    encoded = frame.serialize()

    assert len(encoded) == MEDIA_HEADER_BYTES + 4
    assert WssMediaFrame.parse(encoded) == frame

    with pytest.raises(RvaBindingError) as truncated:
        WssMediaFrame.parse(encoded[: MEDIA_HEADER_BYTES - 1])
    assert truncated.value.code == "truncated_media_header"
    with pytest.raises(RvaBindingError) as too_large:
        WssMediaFrame.parse(b"x" * (MEDIA_MAX_FRAME_BYTES + 1))
    assert too_large.value.code == "media_frame_too_large"
    with pytest.raises(RvaBindingError) as payload_too_large:
        WssMediaFrame(frame.media_id, 7, 0, 0, 0, b"x" * (MEDIA_MAX_PAYLOAD_BYTES + 1)).serialize()
    assert payload_too_large.value.code == "invalid_media_payload"
    with pytest.raises(RvaBindingError) as non_binary:
        WssMediaFrame.parse(bytearray(encoded))  # type: ignore[arg-type]
    assert non_binary.value.code == "media_frame_must_be_binary"
    wrong_length = bytearray(encoded)
    wrong_length[28:32] = (5).to_bytes(4, "big")
    with pytest.raises(RvaBindingError) as length_mismatch:
        WssMediaFrame.parse(bytes(wrong_length))
    assert length_mismatch.value.code == "media_length_mismatch"


@pytest.mark.contract
async def test_uplink_requires_matching_identity_zero_generation_and_exact_sequence() -> None:
    binding, audio, _ = create_binding()
    await open_binding(binding)
    valid = WssMediaFrame(bytes.fromhex("0123456789abcdef"), 7, 0, 0, 0, b"opus").serialize()

    await binding.receive_media(valid)

    assert audio.packets == [InboundAudioPacket(sequence=0, timestamp=0, payload=b"opus")]
    with pytest.raises(RvaBindingError) as duplicate:
        await binding.receive_media(valid)
    assert duplicate.value.code == "invalid_media_sequence"

    wrong_epoch = WssMediaFrame(bytes.fromhex("0123456789abcdef"), 8, 1, 960, 0, b"opus").serialize()
    with pytest.raises(RvaBindingError) as stale:
        await binding.receive_media(wrong_epoch)
    assert stale.value.code == "stale_media_identity"

    wrong_generation = WssMediaFrame(bytes.fromhex("0123456789abcdef"), 7, 1, 960, 1, b"opus").serialize()
    with pytest.raises(RvaBindingError) as generation:
        await binding.receive_media(wrong_generation)
    assert generation.value.code == "invalid_uplink_generation"


@pytest.mark.contract
async def test_transcript_and_response_events_are_ordered_and_generation_fenced() -> None:
    binding, _, _ = create_binding()
    await open_binding(binding)

    delta = json.loads(binding.transcript_delta(utterance_id="utt-001", sequence=0, text="hello"))
    final = json.loads(binding.transcript_final(utterance_id="utt-001", sequence=1, text="hello world"))
    target, begin_raw = await binding.response_begin(response_id="resp-001")
    text = json.loads(
        binding.response_text(response_id="resp-001", target=target, sequence=0, text="Hi")
    )
    media = WssMediaFrame.parse(binding.serialize_audio(b"opus", timestamp=0, target=target))
    end = json.loads(await binding.response_end(response_id="resp-001", target=target))
    error = json.loads(binding.session_error(code="provider_timeout", retryable=True, message="retry later"))

    assert delta["type"] == "transcript.delta"
    assert final["type"] == "transcript.final"
    assert json.loads(begin_raw)["generation"] == target.generation
    assert text["sequence"] == 0
    assert media.generation == target.generation
    assert media.sequence == 0
    assert end["reason"] == "completed"
    assert error["code"] == "provider_timeout"
    assert error["retryable"] is True
    with pytest.raises(RvaBindingError) as stale:
        binding.serialize_audio(b"late", timestamp=960, target=target)
    assert stale.value.code == "stale_generation"


@pytest.mark.contract
async def test_duplicate_transcript_and_response_sequences_are_rejected() -> None:
    binding, _, _ = create_binding()
    await open_binding(binding)
    binding.transcript_delta(utterance_id="utt-001", sequence=0, text="a")
    with pytest.raises(RvaBindingError) as transcript:
        binding.transcript_delta(utterance_id="utt-001", sequence=0, text="duplicate")
    assert transcript.value.code == "invalid_sequence"
    with pytest.raises(RvaBindingError) as text_limit:
        binding.transcript_delta(utterance_id="utt-001", sequence=1, text="x" * 4_097)
    assert text_limit.value.code == "text_too_large"

    target, _ = await binding.response_begin(response_id="resp-001")
    binding.response_text(response_id="resp-001", target=target, sequence=0, text="a")
    with pytest.raises(RvaBindingError) as response:
        binding.response_text(response_id="resp-001", target=target, sequence=0, text="duplicate")
    assert response.value.code == "invalid_sequence"
    with pytest.raises(RvaBindingError) as overlap:
        await binding.response_begin(response_id="resp-002")
    assert overlap.value.code == "response_already_active"


@pytest.mark.contract
async def test_cancel_requires_exact_target_and_interrupts_once() -> None:
    binding, _, agent = create_binding()
    await open_binding(binding)
    target, _ = await binding.response_begin(response_id="resp-001")
    stale_cancel = {
        "type": "response.cancel",
        "session_id": "session-001",
        "session_epoch": "grant-epoch-001",
        "target": {"response_id": "resp-001", "generation": target.generation + 1},
        "reason": "barge_in",
    }
    with pytest.raises(RvaBindingError) as stale:
        await binding.receive_control(json.dumps(stale_cancel))
    assert stale.value.code == "stale_generation"
    assert agent.interrupts == []

    stale_cancel["target"]["generation"] = target.generation
    cancelled_raw = await binding.receive_control(json.dumps(stale_cancel))
    assert cancelled_raw is not None
    cancelled = json.loads(cancelled_raw)
    assert cancelled["target"] == {"response_id": "resp-001", "generation": target.generation}
    assert agent.interrupts == [target]

    with pytest.raises(RvaBindingError) as repeated:
        await binding.receive_control(json.dumps(stale_cancel))
    assert repeated.value.code == "stale_generation"
    assert agent.interrupts == [target]


@pytest.mark.contract
async def test_stale_session_unknown_message_and_preopen_media_fail_explicitly() -> None:
    binding, _, _ = create_binding()
    media = WssMediaFrame(bytes.fromhex("0123456789abcdef"), 7, 0, 0, 0, b"opus").serialize()
    with pytest.raises(RvaBindingError) as preopen:
        await binding.receive_media(media)
    assert preopen.value.code == "session_not_open"

    await open_binding(binding)
    stale_cancel = {
        "type": "response.cancel",
        "session_id": "session-001",
        "session_epoch": "stale-epoch",
        "target": {"response_id": "resp-001", "generation": 1},
        "reason": "barge_in",
    }
    with pytest.raises(RvaBindingError) as stale:
        await binding.receive_control(json.dumps(stale_cancel))
    assert stale.value.code == "stale_session"
    with pytest.raises(RvaBindingError) as unknown:
        await binding.receive_control('{"type":"listen"}')
    assert unknown.value.code == "unknown_client_message"

    invalid_reason = dict(stale_cancel)
    invalid_reason["session_epoch"] = "grant-epoch-001"
    invalid_reason["reason"] = []
    with pytest.raises(RvaBindingError) as malformed:
        await binding.receive_control(json.dumps(invalid_reason))
    assert malformed.value.code == "invalid_cancel_reason"


@pytest.mark.contract
async def test_close_is_terminal_and_closes_injected_ports_once() -> None:
    binding, audio, agent = create_binding()
    await open_binding(binding)
    close = {
        "type": "session.close",
        "session_id": "session-001",
        "session_epoch": "grant-epoch-001",
        "reason": "normal",
        "initiated_by": "device",
        "detail": "user ended the session",
    }

    response_raw = await binding.receive_control(json.dumps(close))

    assert response_raw is not None
    assert json.loads(response_raw)["initiated_by"] == "server"
    assert binding.closed is True
    assert audio.close_calls == 1
    assert agent.close_calls == 1
    await binding.close()
    assert audio.close_calls == 1
    assert agent.close_calls == 1
    with pytest.raises(RvaBindingError) as repeated:
        await binding.receive_control(json.dumps(close))
    assert repeated.value.code == "session_closed"
