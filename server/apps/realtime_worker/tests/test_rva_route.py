from __future__ import annotations

import asyncio
import base64
import json
import logging
import socket
import time

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi.testclient import TestClient
from realtime_worker.app import create_app
from realtime_worker.audio import PCM_SAMPLES, PcmFrame
from realtime_worker.bindings.rva import RvaOpusCodec
from realtime_worker.config import Settings
from realtime_worker.transport.udp_gateway import (
    UDP_FLAG_PROBE,
    UDP_FLAG_PROBE_ACK,
    UdpPacketHeader,
)
from starlette.websockets import WebSocketDisconnect
from voice_contracts import ConnectGrantClaims, GrantCodec


def settings(**changes: object) -> Settings:
    values: dict[str, object] = {
        "worker_id": "worker-a",
        "lab_token": "lab-test-token",
        "grant_signing_key": "validator-grant-signing-key-with-32-bytes",
        "internal_token": "validator-internal-token",
        "runner": "deterministic",
        "director_url": "",
        "heartbeat_enabled": False,
    }
    values.update(changes)
    return Settings(_env_file=None, **values)


def session_open() -> dict[str, object]:
    return {
        "type": "session.open",
        "protocol_version": 1,
        "request_id": "open-001",
        "device_id": "device-001",
        "supported_media_profiles": ["wss-opus-v2"],
        "preferred_media_profile": "wss-opus-v2",
        "audio": {"codec": "opus", "sample_rate_hz": 16_000, "channels": 1, "frame_duration_ms": 60},
        "capabilities": {"aec": True, "vad": True},
    }


def connect_grant(*, control_protocol: str, profiles: tuple[str, ...]) -> str:
    now = time.time()
    claims = ConnectGrantClaims(
        tenant_id="tenant-001",
        device_id="device-001",
        worker_id="worker-a",
        session_epoch="grant-epoch-001",
        fencing_token=2,
        profiles=profiles,
        control_protocol=control_protocol,
        iat=now,
        exp=now + 30,
        jti=f"jti-{control_protocol}",
    )
    return GrantCodec("validator-grant-signing-key-with-32-bytes").issue(claims)


class FakeGrantConsumer:
    def __init__(self, accepted: bool = True) -> None:
        self.accepted = accepted
        self.calls: list[tuple[str, str]] = []

    async def consume(self, token: str, *, device_id: str) -> bool:
        self.calls.append((token, device_id))
        return self.accepted

    async def close(self) -> None:
        return None


class FatalGrantConsume(BaseException):
    pass


def test_rva_lab_route_completes_native_handshake(caplog: pytest.LogCaptureFixture) -> None:
    app = create_app(settings())
    caplog.set_level(logging.INFO, logger="realtime_worker.app")
    caplog.set_level(logging.INFO, logger="realtime_worker.bindings.rva.runtime")
    with TestClient(app) as client:
        with client.websocket_connect(
            "/v1/voice",
            headers={"Authorization": "Bearer lab-test-token", "Device-Id": "device-001"},
        ) as websocket:
            websocket.send_json(session_open())
            opened = websocket.receive_json()
            assert opened["type"] == "session.opened"
            assert opened["session_epoch"].startswith("lab-")
            assert opened["selected_media_profile"] == "wss-opus-v2"
            assert app.state.admission.active_count == 1
    assert app.state.admission.active_count == 0
    assert "worker_websocket_accepted binding=rva-control-v1" in caplog.text
    assert "rva_session_opened" in caplog.text
    assert "selected_media_profile=wss-opus-v2" in caplog.text
    assert "close_reason=normal" in caplog.text
    assert "lab-test-token" not in caplog.text
    assert "device-001" not in caplog.text


def test_rva_udp_handshake_uses_schema_grant_and_canonical_probe_ack() -> None:
    app = create_app(
        settings(
            rva_udp_enabled=True,
            udp_bind_host="127.0.0.1",
            udp_bind_port=0,
            udp_advertise_host="127.0.0.1",
            udp_probe_timeout_seconds=1,
        )
    )
    opened_request = session_open()
    opened_request["supported_media_profiles"] = ["udp-opus-gcm-v1", "wss-opus-v2"]
    opened_request["preferred_media_profile"] = "udp-opus-gcm-v1"
    with (
        TestClient(app) as client,
        client.websocket_connect(
            "/v1/voice",
            headers={"Authorization": "Bearer lab-test-token", "Device-Id": "device-001"},
        ) as websocket,
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp,
    ):
        udp.settimeout(2)
        websocket.send_json(opened_request)
        opened = websocket.receive_json()
        assert opened["selected_media_profile"] == "udp-opus-gcm-v1"
        grant = opened["udp_grant"]
        assert set(grant) == {
            "host",
            "port",
            "expires_at_ms",
            "uplink_key_b64",
            "uplink_salt_b64",
            "downlink_key_b64",
            "downlink_salt_b64",
            "probe_timeout_ms",
        }
        media_id = bytes.fromhex(opened["media_id"])
        header = UdpPacketHeader(
            flags=UDP_FLAG_PROBE,
            media_id=media_id,
            media_epoch=opened["media_epoch"],
            sequence=0,
            timestamp=0,
            generation=0,
            payload_length=0,
        )
        aad = header.encode()
        key = base64.b64decode(grant["uplink_key_b64"])
        salt = base64.b64decode(grant["uplink_salt_b64"])
        udp.sendto(aad + AESGCM(key).encrypt(salt + b"\x00\x00\x00\x00", b"", aad), (grant["host"], grant["port"]))
        ack, _ = udp.recvfrom(1_280)
        ack_header, encrypted = UdpPacketHeader.decode(ack)
        downlink_key = base64.b64decode(grant["downlink_key_b64"])
        downlink_salt = base64.b64decode(grant["downlink_salt_b64"])
        payload = AESGCM(downlink_key).decrypt(
            downlink_salt + ack_header.sequence.to_bytes(4, "big"), encrypted, ack[:32]
        )
        assert ack_header.flags == UDP_FLAG_PROBE_ACK
        assert ack_header.generation == 1
        assert payload == b""

        codec = RvaOpusCodec()
        opus = codec.encode_60ms(
            [PcmFrame(0, index, index * PCM_SAMPLES, b"\x00" * (PCM_SAMPLES * 2)) for index in range(3)]
        )
        audio_header = UdpPacketHeader(
            flags=1,
            media_id=media_id,
            media_epoch=opened["media_epoch"],
            sequence=1,
            timestamp=960,
            generation=1,
            payload_length=len(opus),
        )
        audio_aad = audio_header.encode()
        encrypted_audio = AESGCM(key).encrypt(salt + b"\x00\x00\x00\x01", opus, audio_aad)
        udp.sendto(audio_aad + encrypted_audio, (grant["host"], grant["port"]))

        controls: list[dict[str, object]] = []
        while not any(event.get("type") == "response.begin" for event in controls):
            controls.append(websocket.receive_json())
        begin = next(event for event in controls if event.get("type") == "response.begin")
        downlink_packets = 0
        while downlink_packets < 4:
            datagram, _ = udp.recvfrom(1_280)
            media_header, encrypted = UdpPacketHeader.decode(datagram)
            AESGCM(downlink_key).decrypt(
                downlink_salt + media_header.sequence.to_bytes(4, "big"), encrypted, datagram[:32]
            )
            if media_header.flags == 1:
                assert media_header.generation == begin["generation"]
                downlink_packets += 1
        while not any(event.get("type") == "response.end" for event in controls):
            controls.append(websocket.receive_json())
        assert any(event.get("type") == "response.text" for event in controls)


def test_rva_director_grant_is_consumed_once_after_protocol_binding() -> None:
    consumer = FakeGrantConsumer()
    app = create_app(
        settings(),
        grant_consumer=consumer,  # type: ignore[arg-type]
    )
    token = connect_grant(control_protocol="rva-control-v1", profiles=("wss-opus-v2",))
    with TestClient(app) as client:
        with client.websocket_connect(
            "/v1/voice",
            headers={"Authorization": f"Bearer {token}", "Device-Id": "device-001"},
        ) as websocket:
            websocket.send_json(session_open())
            assert websocket.receive_json()["session_epoch"] == "grant-epoch-001"
            renewals = app.state.rva_session_registry.active_lease_renewals()
            assert [(item.session_epoch, item.fencing_token) for item in renewals] == [("grant-epoch-001", 2)]
        releases = app.state.rva_session_registry.pending_lease_releases()
        assert [(item.session_epoch, item.fencing_token) for item in releases] == [("grant-epoch-001", 2)]

    assert consumer.calls == [(token, "device-001")]


def test_rva_udp_preference_falls_back_to_wss_when_server_udp_is_disabled() -> None:
    consumer = FakeGrantConsumer()
    app = create_app(settings(rva_udp_enabled=False), grant_consumer=consumer)  # type: ignore[arg-type]
    token = connect_grant(
        control_protocol="rva-control-v1",
        profiles=("wss-opus-v2", "udp-opus-gcm-v1"),
    )
    request = session_open()
    request["supported_media_profiles"] = ["udp-opus-gcm-v1", "wss-opus-v2"]
    request["preferred_media_profile"] = "udp-opus-gcm-v1"
    with TestClient(app) as client:
        with client.websocket_connect(
            "/v1/voice",
            headers={"Authorization": f"Bearer {token}", "Device-Id": "device-001"},
        ) as websocket:
            websocket.send_json(request)
            opened = websocket.receive_json()
            assert opened["selected_media_profile"] == "wss-opus-v2"
            assert "udp_grant" not in opened


def test_rva_route_can_be_disabled() -> None:
    app = create_app(settings(rva_enabled=False, legacy_xiaozhi_enabled=True))

    assert "/v1/voice" not in {route.path for route in app.routes}


def test_xiaozhi_grant_is_rejected_by_rva_route_before_consumption() -> None:
    consumer = FakeGrantConsumer()
    app = create_app(settings(), grant_consumer=consumer)  # type: ignore[arg-type]
    token = connect_grant(control_protocol="xiaozhi-control-v1", profiles=("wss-opus-v1",))
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as rejected:
            with client.websocket_connect(
                "/v1/voice",
                headers={"Authorization": f"Bearer {token}", "Device-Id": "device-001"},
            ):
                pass

    assert rejected.value.code == 1_008
    assert consumer.calls == []


def test_rva_route_rejects_grant_without_wss_v2_before_consumption(caplog: pytest.LogCaptureFixture) -> None:
    consumer = FakeGrantConsumer()
    app = create_app(settings(), grant_consumer=consumer)  # type: ignore[arg-type]
    token = connect_grant(control_protocol="rva-control-v1", profiles=("udp-opus-gcm-v1",))
    caplog.set_level(logging.WARNING, logger="realtime_worker.app")
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as rejected:
            with client.websocket_connect(
                "/v1/voice",
                headers={"Authorization": f"Bearer {token}", "Device-Id": "device-001"},
            ):
                pass

    assert rejected.value.code == 1_008
    assert consumer.calls == []
    assert "reason=no_compatible_profile" in caplog.text
    assert "device_ref=" in caplog.text
    assert "device-001" not in caplog.text
    assert token not in caplog.text


def test_rva_route_fails_closed_when_director_rejects_consumption(caplog: pytest.LogCaptureFixture) -> None:
    consumer = FakeGrantConsumer(accepted=False)
    app = create_app(settings(), grant_consumer=consumer)  # type: ignore[arg-type]
    token = connect_grant(control_protocol="rva-control-v1", profiles=("wss-opus-v2",))
    caplog.set_level(logging.WARNING, logger="realtime_worker.app")
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as rejected:
            with client.websocket_connect(
                "/v1/voice",
                headers={"Authorization": f"Bearer {token}", "Device-Id": "device-001"},
            ):
                pass

    assert rejected.value.code == 1_008
    assert consumer.calls == [(token, "device-001")]
    assert "reason=grant_rejected" in caplog.text
    assert "device_ref=" in caplog.text
    assert "device-001" not in caplog.text
    assert token not in caplog.text


def test_rva_udp_probe_timeout_has_specific_close_reason_and_safe_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app(
        settings(
            rva_udp_enabled=True,
            udp_bind_host="127.0.0.1",
            udp_bind_port=0,
            udp_advertise_host="127.0.0.1",
            udp_probe_timeout_seconds=0.05,
        )
    )
    opened_request = session_open()
    opened_request["supported_media_profiles"] = ["udp-opus-gcm-v1", "wss-opus-v2"]
    opened_request["preferred_media_profile"] = "udp-opus-gcm-v1"
    caplog.set_level(logging.INFO, logger="realtime_worker.bindings.rva.runtime")
    caplog.set_level(logging.INFO, logger="realtime_worker.transport.udp_gateway")
    with TestClient(app) as client:
        with client.websocket_connect(
            "/v1/voice",
            headers={"Authorization": "Bearer lab-test-token", "Device-Id": "device-001"},
        ) as websocket:
            websocket.send_json(opened_request)
            opened = websocket.receive_json()
            assert opened["selected_media_profile"] == "udp-opus-gcm-v1"
            with pytest.raises(WebSocketDisconnect) as closed:
                websocket.receive_json()

    assert closed.value.code == 1_008
    assert closed.value.reason == "udp_probe_timeout"
    assert "rva_session_opened" in caplog.text
    assert "selected_media_profile=udp-opus-gcm-v1" in caplog.text
    assert "udp_wait_ready_started" in caplog.text
    assert "reason=udp_probe_timeout" in caplog.text
    assert "close_reason=udp_probe_timeout" in caplog.text
    assert "lab-test-token" not in caplog.text
    assert "device-001" not in caplog.text
    assert "uplink_key" not in caplog.text
    assert "downlink_key" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["accept", "registration"])
async def test_rva_route_releases_consumed_grant_when_startup_fails(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    consumer = FakeGrantConsumer()
    app = create_app(settings(), grant_consumer=consumer)  # type: ignore[arg-type]
    token = connect_grant(control_protocol="rva-control-v1", profiles=("wss-opus-v2",))

    class FakeWebSocket:
        headers = {"authorization": f"Bearer {token}", "device-id": "device-001"}

        async def accept(self) -> None:
            if failure == "accept":
                raise RuntimeError("accept failed")

        async def close(self, *, code: int, reason: str) -> None:
            pytest.fail(f"unexpected websocket close: {code} {reason}")

    if failure == "registration":
        monkeypatch.setattr(
            "realtime_worker.app.RvaWssConnection",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("registration failed")),
        )
    route = next(route for route in app.routes if route.path == "/v1/voice")

    async with app.router.lifespan_context(app):
        with pytest.raises(RuntimeError, match=f"{failure} failed"):
            await route.endpoint(FakeWebSocket())  # type: ignore[attr-defined, arg-type]

        assert consumer.calls == [(token, "device-001")]
        assert app.state.admission.active_count == 0
        releases = app.state.rva_session_registry.pending_lease_releases()
        assert [(item.session_epoch, item.fencing_token) for item in releases] == [("grant-epoch-001", 2)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route_path", "control_protocol", "registry_state_name"),
    [
        ("/v1/voice", "rva-control-v1", "rva_session_registry"),
        ("/v1/xiaozhi", "xiaozhi-control-v1", "xiaozhi_session_registry"),
    ],
)
@pytest.mark.parametrize("failure_type", [asyncio.CancelledError, FatalGrantConsume])
async def test_grant_consume_interruption_releases_reservation_and_exact_lease_once(
    route_path: str,
    control_protocol: str,
    registry_state_name: str,
    failure_type: type[BaseException],
) -> None:
    class InterruptedGrantConsumer(FakeGrantConsumer):
        async def consume(self, token: str, *, device_id: str) -> bool:
            self.calls.append((token, device_id))
            raise failure_type()

    consumer = InterruptedGrantConsumer()
    app = create_app(
        settings(legacy_xiaozhi_enabled=route_path == "/v1/xiaozhi"),
        grant_consumer=consumer,  # type: ignore[arg-type]
    )
    profiles = ("wss-opus-v1",) if route_path == "/v1/xiaozhi" else ("wss-opus-v2",)
    token = connect_grant(control_protocol=control_protocol, profiles=profiles)

    class FakeWebSocket:
        headers = {
            "authorization": f"Bearer {token}",
            "device-id": "device-001",
            "protocol-version": "1",
        }

        async def close(self, *, code: int, reason: str) -> None:
            pytest.fail(f"unexpected websocket close: {code} {reason}")

    route = next(route for route in app.routes if route.path == route_path)
    async with app.router.lifespan_context(app):
        with pytest.raises(failure_type):
            await route.endpoint(FakeWebSocket())  # type: ignore[attr-defined, arg-type]

        registry = getattr(app.state, registry_state_name)
        assert app.state.admission.active_count == 0
        releases = registry.pending_lease_releases()
        assert [(item.session_epoch, item.fencing_token) for item in releases] == [("grant-epoch-001", 2)]

    assert consumer.calls == [(token, "device-001")]


def test_shared_admission_rejects_same_principal_across_xiaozhi_and_rva() -> None:
    app = create_app(settings(legacy_xiaozhi_enabled=True))
    xiaozhi_hello = {
        "type": "hello",
        "version": 1,
        "transport": "websocket",
        "audio_params": {"format": "opus", "sample_rate": 16_000, "channels": 1, "frame_duration": 60},
        "transport_profiles": ["wss-opus-v1"],
        "transport_mode": "force_wss",
    }
    headers = {"Authorization": "Bearer lab-test-token", "Device-Id": "device-001", "Protocol-Version": "1"}
    with TestClient(app) as client:
        with client.websocket_connect("/v1/xiaozhi", headers=headers) as xiaozhi:
            xiaozhi.send_text(json.dumps(xiaozhi_hello))
            assert xiaozhi.receive_json()["type"] == "hello"
            with pytest.raises(WebSocketDisconnect) as overloaded:
                with client.websocket_connect(
                    "/v1/voice",
                    headers={"Authorization": "Bearer lab-test-token", "Device-Id": "device-001"},
                ):
                    pass
            assert overloaded.value.code == 1_013


def test_capacity_rejection_does_not_consume_director_grant() -> None:
    consumer = FakeGrantConsumer()
    app = create_app(
        settings(max_sessions=1, legacy_xiaozhi_enabled=True),
        grant_consumer=consumer,  # type: ignore[arg-type]
    )
    token = connect_grant(control_protocol="rva-control-v1", profiles=("wss-opus-v2",))
    hello = {
        "type": "hello",
        "version": 1,
        "transport": "websocket",
        "audio_params": {"format": "opus", "sample_rate": 16_000, "channels": 1, "frame_duration": 60},
        "transport_profiles": ["wss-opus-v1"],
        "transport_mode": "force_wss",
    }
    with TestClient(app) as client:
        with client.websocket_connect(
            "/v1/xiaozhi",
            headers={
                "Authorization": "Bearer lab-test-token",
                "Device-Id": "device-other",
                "Protocol-Version": "1",
            },
        ) as occupied:
            occupied.send_json(hello)
            assert occupied.receive_json()["type"] == "hello"
            with pytest.raises(WebSocketDisconnect) as overloaded:
                with client.websocket_connect(
                    "/v1/voice",
                    headers={"Authorization": f"Bearer {token}", "Device-Id": "device-001"},
                ):
                    pass
    assert overloaded.value.code == 1_013
    assert consumer.calls == []


def test_xiaozhi_capacity_rejection_does_not_consume_director_grant() -> None:
    consumer = FakeGrantConsumer()
    app = create_app(
        settings(max_sessions=1, legacy_xiaozhi_enabled=True),
        grant_consumer=consumer,  # type: ignore[arg-type]
    )
    token = connect_grant(control_protocol="xiaozhi-control-v1", profiles=("wss-opus-v1",))
    with TestClient(app) as client:
        with client.websocket_connect(
            "/v1/voice",
            headers={"Authorization": "Bearer lab-test-token", "Device-Id": "device-other"},
        ) as occupied:
            occupied.send_json({**session_open(), "device_id": "device-other"})
            assert occupied.receive_json()["type"] == "session.opened"
            with pytest.raises(WebSocketDisconnect) as overloaded:
                with client.websocket_connect(
                    "/v1/xiaozhi",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Device-Id": "device-001",
                        "Protocol-Version": "1",
                    },
                ):
                    pass

    assert overloaded.value.code == 1_013
    assert consumer.calls == []


def test_xiaozhi_consume_rejection_releases_admission() -> None:
    consumer = FakeGrantConsumer(accepted=False)
    app = create_app(
        settings(max_sessions=1, legacy_xiaozhi_enabled=True),
        grant_consumer=consumer,  # type: ignore[arg-type]
    )
    token = connect_grant(control_protocol="xiaozhi-control-v1", profiles=("wss-opus-v1",))
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as rejected:
            with client.websocket_connect(
                "/v1/xiaozhi",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Device-Id": "device-001",
                    "Protocol-Version": "1",
                },
            ):
                pass
        assert app.state.admission.active_count == 0
        assert app.state.xiaozhi_session_registry.pending_lease_releases() == ()

    assert rejected.value.code == 1_008
    assert consumer.calls == [(token, "device-001")]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["accept", "registration"])
async def test_xiaozhi_route_releases_consumed_grant_when_startup_fails(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    consumer = FakeGrantConsumer()
    app = create_app(
        settings(legacy_xiaozhi_enabled=True),
        grant_consumer=consumer,  # type: ignore[arg-type]
    )
    token = connect_grant(control_protocol="xiaozhi-control-v1", profiles=("wss-opus-v1",))

    class FakeWebSocket:
        headers = {
            "authorization": f"Bearer {token}",
            "device-id": "device-001",
            "protocol-version": "1",
        }

        async def accept(self) -> None:
            if failure == "accept":
                raise RuntimeError("accept failed")

        async def close(self, *, code: int, reason: str) -> None:
            pytest.fail(f"unexpected websocket close: {code} {reason}")

    if failure == "registration":
        monkeypatch.setattr(
            "realtime_worker.bindings.xiaozhi_runtime.XiaozhiConnection",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("registration failed")),
        )
    route = next(route for route in app.routes if route.path == "/v1/xiaozhi")

    async with app.router.lifespan_context(app):
        with pytest.raises(RuntimeError, match=f"{failure} failed"):
            await route.endpoint(FakeWebSocket())  # type: ignore[attr-defined, arg-type]

        assert consumer.calls == [(token, "device-001")]
        assert app.state.admission.active_count == 0
        releases = app.state.xiaozhi_session_registry.pending_lease_releases()
        assert [(item.session_epoch, item.fencing_token) for item in releases] == [("grant-epoch-001", 2)]
