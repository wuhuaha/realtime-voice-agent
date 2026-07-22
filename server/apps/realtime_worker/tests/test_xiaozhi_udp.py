from __future__ import annotations

import asyncio
import base64
import json
import logging
import socket
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi.testclient import TestClient
from realtime_worker.app import create_app
from realtime_worker.audio import PCM_SAMPLES, PcmFrame
from realtime_worker.auth import AuthContext
from realtime_worker.bindings.xiaozhi import XiaozhiConnection, XiaozhiOpusCodec
from realtime_worker.config import Settings
from realtime_worker.transport.udp_gateway import (
    UDP_FLAG_AUDIO,
    UDP_FLAG_KEEPALIVE,
    UDP_FLAG_PROBE,
    UDP_FLAG_PROBE_ACK,
    UDP_HEADER_BYTES,
    UDP_MAX_PAYLOAD_BYTES,
    ReplayWindow,
    UdpMediaError,
    UdpMediaGateway,
    UdpPacketHeader,
)
from realtime_worker.transport.udp_wire import UDP_MAX_SEQUENCE_FORWARD_JUMP

FIXTURES = Path(__file__).parent / "fixtures" / "xiaozhi"


def _legacy_auth() -> AuthContext:
    return AuthContext(
        tenant_id="lab",
        device_id="device-1",
        allowed_profiles=("wss-opus-v1", "udp-opus-gcm-v1"),
        control_protocol="xiaozhi-control-v1",
    )


def _hello() -> dict[str, object]:
    value = json.loads((FIXTURES / "client_hello_v1.json").read_text(encoding="utf-8"))
    value["transport_profiles"] = ["wss-opus-v1", "udp-opus-gcm-v1"]
    value["transport_mode"] = "force_udp_for_test"
    return value


def _headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer test-token",
        "Protocol-Version": "1",
        "Device-Id": "AA:BB:CC:DD:EE:FF",
        "Client-Id": "550e8400-e29b-41d4-a716-446655440000",
    }


def _packet(
    grant: dict[str, object],
    *,
    flags: int,
    sequence: int,
    payload: bytes,
    generation: int = 1,
) -> bytes:
    media_id = bytes.fromhex(str(grant["media_id"]))
    header = UdpPacketHeader(
        flags=flags,
        media_id=media_id,
        media_epoch=int(grant["media_epoch"]),
        sequence=sequence,
        timestamp=sequence * 960,
        generation=generation,
        payload_length=len(payload),
    )
    aad = header.encode()
    salt = base64.b64decode(str(grant["uplink_salt"]))
    key = base64.b64decode(str(grant["uplink_key"]))
    return aad + AESGCM(key).encrypt(salt + sequence.to_bytes(4, "big"), payload, aad)


def _decrypt_downlink(grant: dict[str, object], datagram: bytes) -> tuple[UdpPacketHeader, bytes]:
    header, encrypted = UdpPacketHeader.decode(datagram)
    key = base64.b64decode(str(grant["downlink_key"]))
    salt = base64.b64decode(str(grant["downlink_salt"]))
    payload = AESGCM(key).decrypt(salt + header.sequence.to_bytes(4, "big"), encrypted, datagram[:UDP_HEADER_BYTES])
    return header, payload


def _silent_opus_packet() -> bytes:
    codec = XiaozhiOpusCodec()
    frames = [PcmFrame(1, sequence, sequence * PCM_SAMPLES, b"\x00" * (PCM_SAMPLES * 2)) for sequence in range(3)]
    return codec.encode_60ms(frames)


def test_replay_window_accepts_bounded_reorder_and_rejects_duplicates() -> None:
    window = ReplayWindow()
    for sequence in (4, 2, 3, 7, 6):
        assert window.can_accept(sequence)
        window.commit(sequence)
    assert not window.can_accept(4)
    window.commit(70)
    assert not window.can_accept(2)


def test_replay_window_enforces_canonical_maximum_forward_jump() -> None:
    window = ReplayWindow()
    window.commit(0)
    assert window.can_accept(UDP_MAX_SEQUENCE_FORWARD_JUMP)
    assert not window.can_accept(UDP_MAX_SEQUENCE_FORWARD_JUMP + 1)


def test_legacy_xiaozhi_udp_import_is_only_a_neutral_transport_alias() -> None:
    from realtime_worker.bindings.xiaozhi_udp import UdpMediaGateway as LegacyGateway

    assert LegacyGateway is UdpMediaGateway


@pytest.mark.asyncio
async def test_udp_gateway_rejects_nonzero_initial_probe_without_binding_source() -> None:
    gateway = UdpMediaGateway(
        bind_host="127.0.0.1",
        bind_port=0,
        advertised_host="127.0.0.1",
        lifetime_seconds=60,
        probe_timeout_seconds=1,
        queue_size=8,
        reorder_wait_seconds=0.05,
    )
    await gateway.start()
    session = gateway.create_session(lambda *_: asyncio.sleep(0), lambda exc: None)
    grant = session.grant.as_control_payload()
    endpoint = (str(grant["server"]), int(grant["port"]))
    loop = asyncio.get_running_loop()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.setblocking(False)
        await loop.sock_sendto(
            udp,
            _packet(grant, flags=UDP_FLAG_PROBE, sequence=1, payload=b""),
            endpoint,
        )
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(loop.sock_recvfrom(udp, 1500), timeout=0.05)
    assert session._source is None  # noqa: SLF001
    assert session.stats.invalid == 1
    await gateway.close()


def test_packet_header_rejects_length_mismatch_before_aead() -> None:
    header = UdpPacketHeader(UDP_FLAG_AUDIO, b"12345678", 1, 1, 960, 1, 4).encode()
    with pytest.raises(ValueError, match="length"):
        UdpPacketHeader.decode(header + b"x" * 16)


def test_packet_header_rejects_payload_above_profile_limit() -> None:
    payload_length = UDP_MAX_PAYLOAD_BYTES + 1
    header = UdpPacketHeader(UDP_FLAG_AUDIO, b"12345678", 1, 1, 960, 1, payload_length).encode()
    with pytest.raises(ValueError, match="too large"):
        UdpPacketHeader.decode(header + b"x" * (payload_length + 16))


@pytest.mark.asyncio
async def test_udp_gateway_can_advertise_a_relay_port_distinct_from_its_bind_port() -> None:
    async def receive(payload: bytes, timestamp: int, generation: int) -> None:
        del payload, timestamp, generation

    gateway = UdpMediaGateway(
        bind_host="127.0.0.1",
        bind_port=0,
        advertised_host="192.0.2.1",
        advertised_port=18093,
        lifetime_seconds=60,
        probe_timeout_seconds=1,
        queue_size=8,
        reorder_wait_seconds=0.05,
    )
    await gateway.start()
    session = gateway.create_session(receive, lambda exc: None)
    try:
        assert session.grant.host == "192.0.2.1"
        assert session.grant.port == 18093
        assert gateway.local_port != 18093
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_udp_gateway_reorders_authenticated_packets_within_deadline() -> None:
    received: list[bytes] = []
    failures: list[BaseException] = []

    async def receive(payload: bytes, timestamp: int, generation: int) -> None:
        del timestamp, generation
        received.append(payload)

    gateway = UdpMediaGateway(
        bind_host="127.0.0.1",
        bind_port=0,
        advertised_host="127.0.0.1",
        lifetime_seconds=60,
        probe_timeout_seconds=1,
        queue_size=8,
        reorder_wait_seconds=0.05,
    )
    await gateway.start()
    session = gateway.create_session(receive, failures.append)
    grant = session.grant.as_control_payload()
    endpoint = (str(grant["server"]), int(grant["port"]))
    loop = asyncio.get_running_loop()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.setblocking(False)
        await loop.sock_sendto(udp, _packet(grant, flags=UDP_FLAG_PROBE, sequence=0, payload=b""), endpoint)
        await asyncio.wait_for(loop.sock_recvfrom(udp, 1500), timeout=1)
        await session.wait_ready(1)
        await loop.sock_sendto(udp, _packet(grant, flags=UDP_FLAG_AUDIO, sequence=2, payload=b"two"), endpoint)
        await loop.sock_sendto(udp, _packet(grant, flags=UDP_FLAG_AUDIO, sequence=1, payload=b"one"), endpoint)
        for _ in range(20):
            if len(received) == 2:
                break
            await asyncio.sleep(0.01)
    await gateway.close()
    assert received == [b"one", b"two"]
    assert failures == []
    assert session.stats.reordered == 1


@pytest.mark.asyncio
async def test_udp_gateway_reacks_authenticated_probe_retry() -> None:
    async def receive(payload: bytes, timestamp: int, generation: int) -> None:
        del payload, timestamp, generation

    gateway = UdpMediaGateway(
        bind_host="127.0.0.1",
        bind_port=0,
        advertised_host="127.0.0.1",
        lifetime_seconds=60,
        probe_timeout_seconds=1,
        queue_size=8,
        reorder_wait_seconds=0.05,
    )
    await gateway.start()
    session = gateway.create_session(receive, lambda exc: None)
    grant = session.grant.as_control_payload()
    endpoint = (str(grant["server"]), int(grant["port"]))
    loop = asyncio.get_running_loop()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.setblocking(False)
        for sequence in (0, 1):
            await loop.sock_sendto(
                udp,
                _packet(grant, flags=UDP_FLAG_PROBE, sequence=sequence, payload=b""),
                endpoint,
            )
            ack, _ = await asyncio.wait_for(loop.sock_recvfrom(udp, 1500), timeout=1)
            header, payload = _decrypt_downlink(grant, ack)
            assert header.flags == UDP_FLAG_PROBE_ACK
            assert payload == b""
        await session.wait_ready(1)
    await gateway.close()
    assert session.stats.invalid == 0


@pytest.mark.asyncio
async def test_udp_gateway_echoes_authenticated_keepalive_for_path_liveness() -> None:
    gateway = UdpMediaGateway(
        bind_host="127.0.0.1",
        bind_port=0,
        advertised_host="127.0.0.1",
        lifetime_seconds=60,
        probe_timeout_seconds=1,
        queue_size=8,
        reorder_wait_seconds=0.05,
    )
    await gateway.start()
    session = gateway.create_session(lambda *_: asyncio.sleep(0), lambda exc: None)
    grant = session.grant.as_control_payload()
    endpoint = (str(grant["server"]), int(grant["port"]))
    loop = asyncio.get_running_loop()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.setblocking(False)
            await loop.sock_sendto(udp, _packet(grant, flags=UDP_FLAG_PROBE, sequence=0, payload=b""), endpoint)
            await asyncio.wait_for(loop.sock_recvfrom(udp, 1500), timeout=1)
            await session.wait_ready(1)

            await loop.sock_sendto(
                udp,
                _packet(grant, flags=UDP_FLAG_KEEPALIVE, sequence=1, payload=b"", generation=7),
                endpoint,
            )
            reply, source = await asyncio.wait_for(loop.sock_recvfrom(udp, 1500), timeout=1)
            header, payload = _decrypt_downlink(grant, reply)

        assert source == endpoint
        assert header.flags == UDP_FLAG_KEEPALIVE
        assert header.generation == 7
        assert header.timestamp == 960
        assert payload == b""
        assert session.stats.authenticated == 2
        assert session.stats.sent == 2
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_udp_gateway_late_probe_retry_does_not_rewind_audio_cursor() -> None:
    received: list[bytes] = []

    async def receive(payload: bytes, timestamp: int, generation: int) -> None:
        del timestamp, generation
        received.append(payload)

    gateway = UdpMediaGateway(
        bind_host="127.0.0.1",
        bind_port=0,
        advertised_host="127.0.0.1",
        lifetime_seconds=60,
        probe_timeout_seconds=1,
        queue_size=8,
        reorder_wait_seconds=0.05,
    )
    await gateway.start()
    session = gateway.create_session(receive, lambda exc: None)
    grant = session.grant.as_control_payload()
    endpoint = (str(grant["server"]), int(grant["port"]))
    loop = asyncio.get_running_loop()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.setblocking(False)
        for sequence in (0, 2, 1):
            await loop.sock_sendto(
                udp,
                _packet(grant, flags=UDP_FLAG_PROBE, sequence=sequence, payload=b""),
                endpoint,
            )
            await asyncio.wait_for(loop.sock_recvfrom(udp, 1500), timeout=1)
        await loop.sock_sendto(
            udp,
            _packet(grant, flags=UDP_FLAG_AUDIO, sequence=3, payload=b"audio"),
            endpoint,
        )
        for _ in range(20):
            if received:
                break
            await asyncio.sleep(0.01)
    await gateway.close()
    assert received == [b"audio"]


@pytest.mark.asyncio
async def test_probe_retry_cannot_skip_buffered_audio_or_leave_reorder_timer_running() -> None:
    received: list[bytes] = []

    async def receive(payload: bytes, timestamp: int, generation: int) -> None:
        del timestamp, generation
        received.append(payload)

    gateway = UdpMediaGateway(
        bind_host="127.0.0.1",
        bind_port=0,
        advertised_host="127.0.0.1",
        lifetime_seconds=60,
        probe_timeout_seconds=1,
        queue_size=8,
        reorder_wait_seconds=0.02,
    )
    await gateway.start()
    session = gateway.create_session(receive, lambda exc: None)
    grant = session.grant.as_control_payload()
    endpoint = (str(grant["server"]), int(grant["port"]))
    loop = asyncio.get_running_loop()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.setblocking(False)
        await loop.sock_sendto(
            udp,
            _packet(grant, flags=UDP_FLAG_PROBE, sequence=0, payload=b""),
            endpoint,
        )
        await asyncio.wait_for(loop.sock_recvfrom(udp, 1500), timeout=1)
        await session.wait_ready(1)
        await loop.sock_sendto(
            udp,
            _packet(grant, flags=UDP_FLAG_AUDIO, sequence=2, payload=b"audio-two"),
            endpoint,
        )
        await loop.sock_sendto(
            udp,
            _packet(grant, flags=UDP_FLAG_PROBE, sequence=3, payload=b""),
            endpoint,
        )
        await asyncio.wait_for(loop.sock_recvfrom(udp, 1500), timeout=1)
        for _ in range(30):
            if received and not session._reorder:  # noqa: SLF001
                break
            await asyncio.sleep(0.01)

    assert received == [b"audio-two"]
    assert session.stats.lost == 1
    assert session._next_audio_sequence == 4  # noqa: SLF001
    assert session._reorder == {}  # noqa: SLF001
    assert session._reorder_timer is None  # noqa: SLF001
    await gateway.close()


@pytest.mark.asyncio
async def test_udp_session_expiry_reports_terminal_failure_without_media() -> None:
    failures: list[BaseException] = []

    async def receive(payload: bytes, timestamp: int, generation: int) -> None:
        del payload, timestamp, generation

    gateway = UdpMediaGateway(
        bind_host="127.0.0.1",
        bind_port=0,
        advertised_host="127.0.0.1",
        lifetime_seconds=0,
        probe_timeout_seconds=1,
        queue_size=8,
        reorder_wait_seconds=0.05,
    )
    await gateway.start()
    gateway.create_session(receive, failures.append)
    for _ in range(20):
        if failures:
            break
        await asyncio.sleep(0.01)
    await gateway.close()

    assert len(failures) == 1
    assert isinstance(failures[0], UdpMediaError)
    assert str(failures[0]) == "UDP media session expired"


class _ProbeTimeoutWebSocket:
    def __init__(self) -> None:
        self._first = True
        self.sent: list[dict[str, object]] = []
        self.closed: list[tuple[int, str]] = []

    async def receive(self) -> dict[str, object]:
        if self._first:
            self._first = False
            return {"type": "websocket.receive", "text": json.dumps(_hello())}
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send_json(self, payload: dict[str, object]) -> None:
        self.sent.append(payload)

    async def send_bytes(self, payload: bytes) -> None:
        del payload

    async def close(self, *, code: int, reason: str) -> None:
        self.closed.append((code, reason))


class _DisconnectDuringProbeWebSocket(_ProbeTimeoutWebSocket):
    async def receive(self) -> dict[str, object]:
        if self._first:
            self._first = False
            return {"type": "websocket.receive", "text": json.dumps(_hello())}
        return {"type": "websocket.disconnect", "code": 1000}


class _FakeUdpGrant:
    def as_control_payload(self) -> dict[str, object]:
        return {"server": "127.0.0.1", "port": 18093}


class _ReadyUdpSession:
    def __init__(self) -> None:
        self.grant = _FakeUdpGrant()
        self.closed = False

    async def wait_ready(self, timeout: float) -> None:
        del timeout

    async def close(self) -> None:
        self.closed = True


class _ReadyUdpGateway:
    def __init__(self) -> None:
        self.is_ready = True
        self.session = _ReadyUdpSession()

    def create_session(self, *args: object, **kwargs: object) -> _ReadyUdpSession:
        del args, kwargs
        return self.session


class _SetupRunner:
    def __init__(self, disconnect_stage: str, start_entered: asyncio.Event) -> None:
        self._disconnect_stage = disconnect_stage
        self._start_entered = start_entered
        self.closed = False
        self.start_cancelled = False

    async def start(self) -> None:
        if self._disconnect_stage != "runner_start":
            return
        self._start_entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.start_cancelled = True
            raise

    async def close(self) -> None:
        self.closed = True


class _CancelResistantSetupRunner:
    def __init__(self, cancel_policy: str, start_entered: asyncio.Event) -> None:
        self._cancel_policy = cancel_policy
        self._start_entered = start_entered
        self._release = asyncio.Event()
        self.cancel_count = 0
        self.closed = False

    async def start(self) -> None:
        self._start_entered.set()
        while not self._release.is_set():
            try:
                await self._release.wait()
            except asyncio.CancelledError:
                self.cancel_count += 1
                if self._cancel_policy == "first" and self.cancel_count > 1:
                    raise

    async def close(self) -> None:
        self.closed = True
        self._release.set()


class _DisconnectDuringSetupWebSocket(_ProbeTimeoutWebSocket):
    def __init__(self, disconnect_stage: str, start_entered: asyncio.Event) -> None:
        super().__init__()
        self._disconnect_stage = disconnect_stage
        self._start_entered = start_entered
        self.ready_send_entered = asyncio.Event()
        self.ready_send_cancelled = False

    async def receive(self) -> dict[str, object]:
        if self._first:
            self._first = False
            return {"type": "websocket.receive", "text": json.dumps(_hello())}
        event = self._start_entered if self._disconnect_stage == "runner_start" else self.ready_send_entered
        await event.wait()
        return {"type": "websocket.disconnect", "code": 1000}

    async def send_json(self, payload: dict[str, object]) -> None:
        self.sent.append(payload)
        if payload.get("type") == "media" and payload.get("state") == "ready":
            self.ready_send_entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.ready_send_cancelled = True
                raise


@pytest.mark.asyncio
async def test_probe_timeout_closes_without_constructing_agent_runner() -> None:
    gateway = UdpMediaGateway(
        bind_host="127.0.0.1",
        bind_port=0,
        advertised_host="127.0.0.1",
        lifetime_seconds=60,
        probe_timeout_seconds=0.01,
        queue_size=8,
        reorder_wait_seconds=0.01,
    )
    await gateway.start()
    constructed = 0

    def runner_factory(*args: object, **kwargs: object) -> object:
        del args, kwargs
        nonlocal constructed
        constructed += 1
        raise AssertionError("runner must not be constructed before UDP probe")

    websocket = _ProbeTimeoutWebSocket()
    connection = XiaozhiConnection(
        websocket,  # type: ignore[arg-type]
        _legacy_auth(),
        Settings(
            lab_token="test-token",
            xiaozhi_udp_enabled=True,
            udp_advertise_host="127.0.0.1",
            xiaozhi_transport_policy="force_udp_for_test",
            udp_probe_timeout_seconds=0.01,
        ),
        runner_factory=runner_factory,  # type: ignore[arg-type]
        udp_gateway=gateway,
    )
    await connection.run()
    await gateway.close()
    assert constructed == 0
    assert websocket.sent[0]["transport"] == "udp"
    assert websocket.closed == [(1008, "handshake_timeout")]


@pytest.mark.asyncio
async def test_disconnect_during_probe_never_constructs_agent_runner() -> None:
    gateway = UdpMediaGateway(
        bind_host="127.0.0.1",
        bind_port=0,
        advertised_host="127.0.0.1",
        lifetime_seconds=60,
        probe_timeout_seconds=1,
        queue_size=8,
        reorder_wait_seconds=0.01,
    )
    await gateway.start()
    constructed = 0

    def runner_factory(*args: object, **kwargs: object) -> object:
        del args, kwargs
        nonlocal constructed
        constructed += 1
        raise AssertionError("runner must not be constructed after control disconnect")

    websocket = _DisconnectDuringProbeWebSocket()
    connection = XiaozhiConnection(
        websocket,  # type: ignore[arg-type]
        _legacy_auth(),
        Settings(
            lab_token="test-token",
            xiaozhi_udp_enabled=True,
            udp_advertise_host="127.0.0.1",
            xiaozhi_transport_policy="force_udp_for_test",
        ),
        runner_factory=runner_factory,  # type: ignore[arg-type]
        udp_gateway=gateway,
    )
    await connection.run()
    await gateway.close()

    assert constructed == 0
    assert gateway.session_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("disconnect_stage", ["runner_start", "media_ready"])
async def test_disconnect_during_udp_setup_closes_partial_runner(disconnect_stage: str) -> None:
    start_entered = asyncio.Event()
    websocket = _DisconnectDuringSetupWebSocket(disconnect_stage, start_entered)
    gateway = _ReadyUdpGateway()
    runners: list[_SetupRunner] = []

    def runner_factory(*args: object, **kwargs: object) -> _SetupRunner:
        del args, kwargs
        runner = _SetupRunner(disconnect_stage, start_entered)
        runners.append(runner)
        return runner

    connection = XiaozhiConnection(
        websocket,  # type: ignore[arg-type]
        _legacy_auth(),
        Settings(
            lab_token="test-token",
            xiaozhi_udp_enabled=True,
            udp_advertise_host="127.0.0.1",
            xiaozhi_transport_policy="force_udp_for_test",
            xiaozhi_handshake_timeout_seconds=0.05,
        ),
        runner_factory=runner_factory,  # type: ignore[arg-type]
        udp_gateway=gateway,  # type: ignore[arg-type]
    )

    await asyncio.wait_for(connection.run(), timeout=0.5)

    assert len(runners) == 1
    assert runners[0].closed is True
    assert gateway.session.closed is True
    assert websocket.closed == [(1000, "normal")]
    if disconnect_stage == "runner_start":
        assert runners[0].start_cancelled is True
        assert not websocket.ready_send_entered.is_set()
    else:
        assert websocket.ready_send_cancelled is True


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_policy", ["first", "forever"])
async def test_disconnect_cleanup_is_bounded_when_runner_start_swallows_cancel(
    cancel_policy: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    start_entered = asyncio.Event()
    websocket = _DisconnectDuringSetupWebSocket("runner_start", start_entered)
    gateway = _ReadyUdpGateway()
    runners: list[_CancelResistantSetupRunner] = []

    def runner_factory(*args: object, **kwargs: object) -> _CancelResistantSetupRunner:
        del args, kwargs
        runner = _CancelResistantSetupRunner(cancel_policy, start_entered)
        runners.append(runner)
        return runner

    connection = XiaozhiConnection(
        websocket,  # type: ignore[arg-type]
        _legacy_auth(),
        Settings(
            lab_token="test-token",
            xiaozhi_udp_enabled=True,
            udp_advertise_host="127.0.0.1",
            xiaozhi_transport_policy="force_udp_for_test",
            xiaozhi_handshake_timeout_seconds=1,
        ),
        runner_factory=runner_factory,  # type: ignore[arg-type]
        udp_gateway=gateway,  # type: ignore[arg-type]
    )

    with caplog.at_level(logging.CRITICAL, logger="realtime_worker.bindings.xiaozhi"):
        run_task = asyncio.create_task(connection.run())
        try:
            done, _ = await asyncio.wait({run_task}, timeout=1.5)
            assert run_task in done
            await run_task
        finally:
            if not run_task.done():
                await connection.close()
                await asyncio.wait_for(run_task, timeout=1)

    await asyncio.sleep(0)
    assert len(runners) == 1
    assert runners[0].closed is True
    assert runners[0].cancel_count >= 1
    assert gateway.session.closed is True
    assert websocket.closed == [(1000, "normal")]
    assert not websocket.ready_send_entered.is_set()
    assert not any(
        task.get_name().startswith("xiaozhi-runner-start-") and not task.done() for task in asyncio.all_tasks()
    )
    if cancel_policy == "first":
        assert "detaching non-cooperative task" not in caplog.text
    else:
        assert "detaching non-cooperative task" in caplog.text


def test_udp_profile_completes_authenticated_opus_turn_and_teardown() -> None:
    settings = Settings(
        lab_token="test-token",
        legacy_xiaozhi_enabled=True,
        xiaozhi_udp_enabled=True,
        udp_bind_host="127.0.0.1",
        udp_bind_port=0,
        udp_advertise_host="127.0.0.1",
        xiaozhi_transport_policy="force_udp_for_test",
        xiaozhi_queue_timeout_seconds=1,
    )
    app = create_app(settings)
    with (
        TestClient(app) as client,
        client.websocket_connect("/v1/xiaozhi", headers=_headers()) as websocket,
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp,
    ):
        udp.settimeout(2)
        websocket.send_json(_hello())
        opened = websocket.receive_json()
        assert opened["transport"] == "udp"
        assert opened["transport_profile"] == "udp-opus-gcm-v1"
        grant = opened["udp"]
        assert isinstance(grant, dict)
        endpoint = (str(grant["server"]), int(grant["port"]))

        # A forged packet must not bind the source or consume sequence zero.
        forged = bytearray(_packet(grant, flags=UDP_FLAG_PROBE, sequence=0, payload=b""))
        forged[-1] ^= 1
        udp.sendto(forged, endpoint)
        udp.sendto(_packet(grant, flags=UDP_FLAG_PROBE, sequence=0, payload=b""), endpoint)
        ack, _ = udp.recvfrom(1500)
        ack_header, ack_payload = _decrypt_downlink(grant, ack)
        assert ack_header.flags == UDP_FLAG_PROBE_ACK
        assert ack_payload == b""
        assert websocket.receive_json() == {
            "session_id": opened["session_id"],
            "type": "media",
            "state": "ready",
            "transport_profile": "udp-opus-gcm-v1",
        }

        websocket.send_json(
            {
                "session_id": opened["session_id"],
                "type": "listen",
                "state": "start",
                "mode": "realtime",
            }
        )
        audio = _packet(
            grant,
            flags=UDP_FLAG_AUDIO,
            sequence=1,
            payload=_silent_opus_packet(),
        )
        udp.sendto(audio, endpoint)
        udp.sendto(audio, endpoint)

        assert websocket.receive_json()["state"] == "sentence_start"
        assert websocket.receive_json()["state"] == "start"
        packets: list[bytes] = []
        deadline = time.monotonic() + 3
        while len(packets) < 4 and time.monotonic() < deadline:
            datagram, _ = udp.recvfrom(1500)
            header, payload = _decrypt_downlink(grant, datagram)
            if header.flags == UDP_FLAG_AUDIO:
                packets.append(payload)
        assert len(packets) == 4
        assert all(0 < len(packet) < 1275 for packet in packets)
        assert websocket.receive_json()["state"] == "stop"


def test_force_udp_fails_closed_when_capability_is_missing() -> None:
    settings = Settings(
        lab_token="test-token",
        legacy_xiaozhi_enabled=True,
        xiaozhi_udp_enabled=True,
        udp_bind_host="127.0.0.1",
        udp_bind_port=0,
        udp_advertise_host="127.0.0.1",
        xiaozhi_transport_policy="force_udp_for_test",
    )
    app = create_app(settings)
    with TestClient(app) as client, client.websocket_connect("/v1/xiaozhi", headers=_headers()) as websocket:
        legacy = json.loads((FIXTURES / "client_hello_v1.json").read_text(encoding="utf-8"))
        websocket.send_json(legacy)
        closed = websocket.receive()
        assert closed["type"] == "websocket.close"
        assert closed["code"] == 1002


def test_auto_policy_honors_client_selected_udp() -> None:
    settings = Settings(
        lab_token="test-token",
        legacy_xiaozhi_enabled=True,
        xiaozhi_udp_enabled=True,
        udp_bind_host="127.0.0.1",
        udp_bind_port=0,
        udp_advertise_host="127.0.0.1",
        xiaozhi_transport_policy="auto",
    )
    app = create_app(settings)
    with TestClient(app) as client, client.websocket_connect("/v1/xiaozhi", headers=_headers()) as websocket:
        websocket.send_json(_hello())
        opened = websocket.receive_json()
        assert opened["transport_profile"] == "udp-opus-gcm-v1"


def test_auto_policy_honors_client_selected_wss() -> None:
    settings = Settings(
        lab_token="test-token",
        legacy_xiaozhi_enabled=True,
        xiaozhi_udp_enabled=True,
        udp_bind_host="127.0.0.1",
        udp_bind_port=0,
        udp_advertise_host="127.0.0.1",
        xiaozhi_transport_policy="auto",
    )
    hello = _hello()
    hello["transport_mode"] = "force_wss"
    app = create_app(settings)
    with TestClient(app) as client, client.websocket_connect("/v1/xiaozhi", headers=_headers()) as websocket:
        websocket.send_json(hello)
        opened = websocket.receive_json()
        assert opened["transport_profile"] == "wss-opus-v1"


def test_force_policy_conflict_and_missing_capability_fail_closed() -> None:
    settings = Settings(
        lab_token="test-token",
        legacy_xiaozhi_enabled=True,
        xiaozhi_udp_enabled=True,
        udp_bind_host="127.0.0.1",
        udp_bind_port=0,
        udp_advertise_host="127.0.0.1",
        xiaozhi_transport_policy="force_wss",
    )
    hello = _hello()
    hello["transport_profiles"] = ["udp-opus-gcm-v1"]
    app = create_app(settings)
    with TestClient(app) as client, client.websocket_connect("/v1/xiaozhi", headers=_headers()) as websocket:
        websocket.send_json(hello)
        closed = websocket.receive()
        assert closed["type"] == "websocket.close"
        assert closed["code"] == 1002


def test_udp_gateway_failure_marks_forced_udp_readiness_unavailable() -> None:
    settings = Settings(
        lab_token="test-token",
        legacy_xiaozhi_enabled=True,
        xiaozhi_udp_enabled=True,
        udp_bind_host="127.0.0.1",
        udp_bind_port=0,
        udp_advertise_host="127.0.0.1",
        xiaozhi_transport_policy="force_udp_for_test",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        gateway = app.state.xiaozhi_udp_gateway
        assert gateway is not None and gateway.is_ready
        gateway.transport_failed(UdpMediaError("socket failed"))
        response = client.get("/health/ready")
        assert response.status_code == 503
        readiness = response.json()
        assert readiness["status"] == "not_ready"
        assert readiness["xiaozhi_udp_ready"] is False
        assert client.get("/health/live").status_code == 200


@pytest.mark.asyncio
async def test_udp_socket_error_is_recoverable_for_two_sessions(caplog: pytest.LogCaptureFixture) -> None:
    failures: list[tuple[str, BaseException]] = []

    async def receive(payload: bytes, timestamp: int, generation: int) -> None:
        del payload, timestamp, generation

    gateway = UdpMediaGateway(
        bind_host="127.0.0.1",
        bind_port=0,
        advertised_host="127.0.0.1",
        lifetime_seconds=60,
        probe_timeout_seconds=1,
        queue_size=8,
        reorder_wait_seconds=0.01,
    )
    await gateway.start()
    first = gateway.create_session(receive, lambda exc: failures.append(("first", exc)))
    second = gateway.create_session(receive, lambda exc: failures.append(("second", exc)))
    protocol = gateway._protocol  # noqa: SLF001
    assert protocol is not None

    with caplog.at_level(logging.WARNING, logger="realtime_worker.transport.udp_gateway"):
        protocol.error_received(OSError("transient UDP send error"))

    assert gateway.is_ready
    assert gateway.session_count == 2
    assert failures == []
    assert "recoverable UDP socket error" in caplog.text
    await first.close()
    await second.close()
    await gateway.close()


@pytest.mark.asyncio
async def test_stale_udp_connection_lost_cannot_kill_restarted_transport() -> None:
    failures: list[BaseException] = []

    async def receive(payload: bytes, timestamp: int, generation: int) -> None:
        del payload, timestamp, generation

    gateway = UdpMediaGateway(
        bind_host="127.0.0.1",
        bind_port=0,
        advertised_host="127.0.0.1",
        lifetime_seconds=60,
        probe_timeout_seconds=1,
        queue_size=8,
        reorder_wait_seconds=0.01,
    )
    await gateway.start()
    stale_protocol = gateway._protocol  # noqa: SLF001
    assert stale_protocol is not None
    gateway.transport_failed(UdpMediaError("old transport failed"))
    await gateway.start()
    current_transport = gateway.transport
    session = gateway.create_session(receive, failures.append)

    stale_protocol.connection_lost(OSError("late callback from old transport"))

    assert gateway.is_ready
    assert gateway.transport is current_transport
    assert gateway.session_count == 1
    assert failures == []
    await session.close()
    await gateway.close()
