from __future__ import annotations

import json

import pytest
from realtime_worker.bindings.rva import RvaBindingError, RvaWssBinding, WssMediaFrame


class _Port:
    async def receive_audio(self, _packet: object) -> None:
        return None

    async def close(self) -> None:
        return None


def _session_open() -> str:
    return json.dumps(
        {
            "type": "session.open",
            "protocol_version": 1,
            "request_id": "open-001",
            "device_id": "device-001",
            "supported_media_profiles": ["wss-opus/1"],
            "preferred_media_profile": "wss-opus/1",
            "audio": {
                "codec": "opus",
                "sample_rate_hz": 16_000,
                "channels": 1,
                "frame_duration_ms": 60,
            },
            "capabilities": {"aec": True, "vad": True},
        }
    )


def _playback_ended(response_id: str, generation: int, *, outcome: str) -> str:
    return json.dumps(
        {
            "type": "playback.ended",
            "session_id": "session-001",
            "session_epoch": "epoch-001",
            "target": {"response_id": response_id, "generation": generation},
            "outcome": outcome,
            "played_samples": 0,
        }
    )


def _cancel_request(response_id: str, generation: int, *, request_id: str) -> str:
    return json.dumps(
        {
            "type": "response.cancel.request",
            "session_id": "session-001",
            "session_epoch": "epoch-001",
            "request_id": request_id,
            "target": {"response_id": response_id, "generation": generation},
            "cause": "user_request",
        }
    )


def _playback_started(response_id: str, generation: int, first_media_sequence: int) -> str:
    return json.dumps(
        {
            "type": "playback.started",
            "session_id": "session-001",
            "session_epoch": "epoch-001",
            "target": {"response_id": response_id, "generation": generation},
            "first_media_sequence": first_media_sequence,
        }
    )


async def _binding() -> RvaWssBinding:
    port = _Port()
    binding = RvaWssBinding(
        expected_device_id="device-001",
        session_id="session-001",
        session_epoch="epoch-001",
        media_id=bytes.fromhex("0123456789abcdef"),
        media_epoch=7,
        audio_port=port,
        agent_port=port,
    )
    opened = await binding.receive_control(_session_open())
    assert json.loads(opened.outbound[0])["selected_media_profile"] == "wss-opus/1"
    return binding


@pytest.mark.contract
async def test_completed_response_retains_physical_playback_until_endpoint_ack() -> None:
    binding = await _binding()
    record, _ = await binding.response_begin(response_id="resp-001", producer_epoch=1)
    binding.reserve_downlink_media(record)
    semantic_end = json.loads(await binding.response_end(record=record))

    assert semantic_end["outcome"] == "completed"
    assert binding.active_response is None
    assert binding.current_playback is record
    with pytest.raises(RvaBindingError, match="playback_already_active"):
        await binding.response_begin(response_id="resp-002", producer_epoch=2)

    stop_effect = await binding.cancel_active_response(cause="recognized_interrupt")

    assert [json.loads(raw)["type"] for raw in stop_effect.outbound] == ["playback.stop"]
    assert stop_effect.interrupt is record
    assert record.outcome == "completed"
    assert record.stop_sent is True

    ended = await binding.receive_control(
        _playback_ended(record.response_id, record.target.generation, outcome="stopped")
    )
    assert ended.playback_ended is not None
    assert binding.current_playback is None

    next_record, begin = await binding.response_begin(response_id="resp-002", producer_epoch=2)
    assert begin is not None
    assert next_record.target.generation > record.target.generation


def test_media_header_rejects_previous_wire_version() -> None:
    frame = WssMediaFrame(
        media_id=bytes.fromhex("0123456789abcdef"),
        media_epoch=7,
        sequence=0,
        timestamp=0,
        generation=0,
        payload=b"opus",
    )
    wire = bytearray(frame.serialize())
    wire[2] = 2

    with pytest.raises(RvaBindingError, match="unsupported_media_header"):
        WssMediaFrame.parse(bytes(wire))


@pytest.mark.contract
async def test_cancel_request_is_idempotent_and_conflicting_reuse_fails() -> None:
    binding = await _binding()
    record, _ = await binding.response_begin(response_id="resp-001", producer_epoch=1)
    binding.reserve_downlink_media(record)
    request = _cancel_request(record.response_id, record.target.generation, request_id="cancel-001")

    first = await binding.receive_control(request)
    repeated = await binding.receive_control(request)

    assert [json.loads(raw)["type"] for raw in first.outbound] == ["playback.stop", "response.end"]
    assert repeated == repeated.__class__()
    conflicting = json.loads(request)
    conflicting["target"]["generation"] += 1
    with pytest.raises(RvaBindingError, match="request_id_conflict"):
        await binding.receive_control(json.dumps(conflicting))


@pytest.mark.contract
async def test_cancel_request_idempotency_ledger_is_bounded() -> None:
    binding = await _binding()
    for index in range(65):
        record, _ = await binding.response_begin(response_id=f"resp-{index:03d}", producer_epoch=index + 1)
        binding.reserve_downlink_media(record)
        await binding.receive_control(
            _cancel_request(record.response_id, record.target.generation, request_id=f"cancel-{index:03d}")
        )
        await binding.receive_control(
            _playback_ended(record.response_id, record.target.generation, outcome="stopped")
        )

    assert len(binding._cancel_requests) == 64  # noqa: SLF001


@pytest.mark.contract
async def test_playback_started_accepts_actual_first_sequence_within_emitted_range() -> None:
    binding = await _binding()
    record, _ = await binding.response_begin(response_id="resp-001", producer_epoch=1)
    for _ in range(3):
        binding.reserve_downlink_media(record)

    effect = await binding.receive_control(
        _playback_started(record.response_id, record.target.generation, first_media_sequence=1)
    )

    assert effect.playback_started is record


@pytest.mark.contract
async def test_completed_playback_requires_positive_played_samples() -> None:
    binding = await _binding()
    record, _ = await binding.response_begin(response_id="resp-001", producer_epoch=1)
    binding.reserve_downlink_media(record)
    await binding.response_end(record=record)
    message = json.loads(_playback_ended(record.response_id, record.target.generation, outcome="completed"))
    message["last_media_sequence"] = record.last_media_sequence

    with pytest.raises(RvaBindingError, match="playback_evidence_mismatch"):
        await binding.receive_control(json.dumps(message))

    message["played_samples"] = 960
    effect = await binding.receive_control(json.dumps(message))
    assert effect.playback_ended is not None


@pytest.mark.contract
async def test_completed_server_response_accepts_endpoint_playback_failure() -> None:
    binding = await _binding()
    record, _ = await binding.response_begin(response_id="resp-001", producer_epoch=1)
    binding.reserve_downlink_media(record)
    await binding.response_end(record=record)

    effect = await binding.receive_control(
        _playback_ended(record.response_id, record.target.generation, outcome="failed")
    )

    assert effect.playback_ended is not None
    assert effect.playback_ended.outcome == "failed"
    assert binding.current_playback is None
    next_record, begin = await binding.response_begin(response_id="resp-002", producer_epoch=2)
    assert begin is not None
    assert next_record.target.generation > record.target.generation
