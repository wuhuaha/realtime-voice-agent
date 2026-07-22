from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from realtime_worker.auth import AuthContext
from realtime_worker.bindings.xiaozhi import XiaozhiConnection
from realtime_worker.config import Settings
from realtime_worker.transport.udp_gateway import (
    UDP_FLAG_AUDIO,
    UDP_FLAG_KEEPALIVE,
    UDP_FLAG_PROBE,
    UdpGrant,
    UdpMediaError,
    UdpMediaSession,
    UdpPacketHeader,
)
from realtime_worker.transport.udp_wire import UDP_JITTER_WINDOW_PACKETS

pytestmark = pytest.mark.integration

_SOURCE = ("192.0.2.10", 41000)


def _legacy_auth() -> AuthContext:
    return AuthContext(
        tenant_id="lab",
        device_id="device-1",
        allowed_profiles=("wss-opus-v1", "udp-opus-gcm-v1"),
        control_protocol="xiaozhi-control-v1",
    )


class _CaptureTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []

    def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
        self.sent.append((data, addr))


class _FakeGateway:
    def __init__(self) -> None:
        self.transport = _CaptureTransport()
        self.removed: list[tuple[bytes, UdpMediaSession]] = []

    def remove(self, media_id: bytes, session: UdpMediaSession) -> None:
        self.removed.append((media_id, session))


class _NoopWebSocket:
    def __init__(self) -> None:
        self.closed: list[tuple[int, str]] = []

    async def close(self, *, code: int, reason: str) -> None:
        self.closed.append((code, reason))


def _grant() -> UdpGrant:
    return UdpGrant(
        media_id=bytes.fromhex("0123456789abcdef"),
        media_epoch=0x10203040,
        uplink_key=bytes.fromhex("000102030405060708090a0b0c0d0e0f"),
        uplink_salt=bytes.fromhex("a0a1a2a3a4a5a6a7"),
        downlink_key=bytes.fromhex("f0f1f2f3f4f5f6f7f8f9fafbfcfdfeff"),
        downlink_salt=bytes.fromhex("b0b1b2b3b4b5b6b7"),
        host="192.0.2.1",
        port=8092,
        expires_at=int(time.time()) + 60,
        probe_timeout_ms=100,
    )


def _uplink_packet(
    grant: UdpGrant,
    *,
    flags: int,
    sequence: int,
    payload: bytes,
    generation: int = 1,
) -> bytes:
    header = UdpPacketHeader(
        flags=flags,
        media_id=grant.media_id,
        media_epoch=grant.media_epoch,
        sequence=sequence,
        timestamp=sequence * 960,
        generation=generation,
        payload_length=len(payload),
    )
    aad = header.encode()
    nonce = grant.uplink_salt + sequence.to_bytes(4, "big")
    return aad + AESGCM(grant.uplink_key).encrypt(nonce, payload, aad)


async def _eventually(predicate: Callable[[], bool], *, timeout: float = 0.25) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition did not become true before deadline")
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_impairment_sequence_recovers_reorder_skips_loss_and_rejects_duplicate_and_late() -> None:
    received: list[tuple[bytes, int, int]] = []
    failures: list[BaseException] = []

    async def receive(payload: bytes, timestamp: int, generation: int) -> None:
        received.append((payload, timestamp, generation))

    gateway = _FakeGateway()
    grant = _grant()
    session = UdpMediaSession(
        gateway,  # type: ignore[arg-type]
        grant,
        receive,
        failures.append,
        queue_size=32,
        reorder_wait_seconds=0.005,
    )
    try:
        session.enqueue(
            _uplink_packet(grant, flags=UDP_FLAG_PROBE, sequence=0, payload=b""),
            _SOURCE,
        )
        await session.wait_ready(0.1)

        # Reordering within the deadline is lossless.
        session.enqueue(_uplink_packet(grant, flags=UDP_FLAG_AUDIO, sequence=2, payload=b"two"), _SOURCE)
        session.enqueue(_uplink_packet(grant, flags=UDP_FLAG_AUDIO, sequence=1, payload=b"one"), _SOURCE)
        await _eventually(lambda: len(received) == 2)

        # An authenticated duplicate cannot advance the replay window twice.
        session.enqueue(_uplink_packet(grant, flags=UDP_FLAG_AUDIO, sequence=2, payload=b"two"), _SOURCE)
        await _eventually(lambda: session.stats.replayed == 1)

        # Missing sequence 3 reaches the deadline; sequence 4 becomes playable.
        session.enqueue(_uplink_packet(grant, flags=UDP_FLAG_AUDIO, sequence=4, payload=b"four"), _SOURCE)
        await _eventually(lambda: len(received) == 3)
        assert session.stats.lost == 1

        # Sequence 3 is authenticated but too late after the playout cursor advanced.
        session.enqueue(_uplink_packet(grant, flags=UDP_FLAG_AUDIO, sequence=3, payload=b"three"), _SOURCE)
        await _eventually(lambda: session.stats.replayed == 2)

        # A small burst with one swapped pair drains in media sequence order.
        for sequence, payload in ((5, b"five"), (7, b"seven"), (6, b"six"), (8, b"eight")):
            session.enqueue(
                _uplink_packet(grant, flags=UDP_FLAG_AUDIO, sequence=sequence, payload=payload),
                _SOURCE,
            )
        await _eventually(lambda: len(received) == 7)

        assert [item[0] for item in received] == [
            b"one",
            b"two",
            b"four",
            b"five",
            b"six",
            b"seven",
            b"eight",
        ]
        assert [item[1] for item in received] == [960, 1920, 3840, 4800, 5760, 6720, 7680]
        assert all(item[2] == 1 for item in received)
        assert session.stats.received == 10
        assert session.stats.authenticated == 9
        assert session.stats.reordered == 3
        assert session.stats.replayed == 2
        assert failures == []
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_keepalive_sequence_does_not_create_a_false_audio_loss() -> None:
    received: list[bytes] = []
    failures: list[BaseException] = []

    async def receive(payload: bytes, timestamp: int, generation: int) -> None:
        del timestamp, generation
        received.append(payload)

    gateway = _FakeGateway()
    grant = _grant()
    session = UdpMediaSession(
        gateway,  # type: ignore[arg-type]
        grant,
        receive,
        failures.append,
        queue_size=8,
        reorder_wait_seconds=0.005,
    )
    try:
        session.enqueue(
            _uplink_packet(grant, flags=UDP_FLAG_PROBE, sequence=0, payload=b""),
            _SOURCE,
        )
        await session.wait_ready(0.1)
        session.enqueue(
            _uplink_packet(grant, flags=UDP_FLAG_KEEPALIVE, sequence=1, payload=b""),
            _SOURCE,
        )
        session.enqueue(
            _uplink_packet(grant, flags=UDP_FLAG_AUDIO, sequence=2, payload=b"audio"),
            _SOURCE,
        )
        await _eventually(lambda: received == [b"audio"])
        await asyncio.sleep(0.01)

        assert session.stats.lost == 0
        assert failures == []
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_expired_session_rejects_downlink_before_allocating_sequence() -> None:
    failures: list[BaseException] = []

    async def receive(payload: bytes, timestamp: int, generation: int) -> None:
        del payload, timestamp, generation

    gateway = _FakeGateway()
    grant = _grant()
    grant = UdpGrant(
        media_id=grant.media_id,
        media_epoch=grant.media_epoch,
        uplink_key=grant.uplink_key,
        uplink_salt=grant.uplink_salt,
        downlink_key=grant.downlink_key,
        downlink_salt=grant.downlink_salt,
        host=grant.host,
        port=grant.port,
        expires_at=int(time.time()) - 1,
        probe_timeout_ms=grant.probe_timeout_ms,
    )
    session = UdpMediaSession(
        gateway,  # type: ignore[arg-type]
        grant,
        receive,
        failures.append,
        queue_size=8,
        reorder_wait_seconds=0.01,
    )
    session._source = _SOURCE  # noqa: SLF001
    session._ready.set()  # noqa: SLF001
    try:
        with pytest.raises(UdpMediaError, match="expired"):
            await session.send_audio(b"audio", timestamp=0, generation=1)
        assert gateway.transport.sent == []
        assert session._downlink_sequence == 0  # noqa: SLF001
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_multiple_loss_gaps_rearm_deadline_and_far_future_packet_does_not_poison_replay() -> None:
    received: list[bytes] = []
    failures: list[BaseException] = []

    async def receive(payload: bytes, timestamp: int, generation: int) -> None:
        del timestamp, generation
        received.append(payload)

    gateway = _FakeGateway()
    grant = _grant()
    session = UdpMediaSession(
        gateway,  # type: ignore[arg-type]
        grant,
        receive,
        failures.append,
        queue_size=16,
        reorder_wait_seconds=0.005,
    )
    try:
        session.enqueue(
            _uplink_packet(grant, flags=UDP_FLAG_PROBE, sequence=0, payload=b""),
            _SOURCE,
        )
        await session.wait_ready(0.1)

        session.enqueue(_uplink_packet(grant, flags=UDP_FLAG_AUDIO, sequence=2, payload=b"two"), _SOURCE)
        session.enqueue(_uplink_packet(grant, flags=UDP_FLAG_AUDIO, sequence=4, payload=b"four"), _SOURCE)
        await _eventually(lambda: received == [b"two", b"four"])
        assert session.stats.lost == 2

        # This authenticated packet is outside the canonical forward window. It
        # must not advance ReplayWindow and block the next legitimate packet.
        session.enqueue(
            _uplink_packet(grant, flags=UDP_FLAG_AUDIO, sequence=1029, payload=b"future"),
            _SOURCE,
        )
        await _eventually(lambda: session.stats.invalid == 1)
        session.enqueue(_uplink_packet(grant, flags=UDP_FLAG_AUDIO, sequence=5, payload=b"five"), _SOURCE)
        await _eventually(lambda: received[-1:] == [b"five"])

        assert received == [b"two", b"four", b"five"]
        assert failures == []
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_reorder_buffer_drops_the_fifth_packet_without_growing() -> None:
    failures: list[BaseException] = []

    async def receive(_payload: bytes, _timestamp: int, _generation: int) -> None:
        pytest.fail("overflow packet must not reach playout")

    session = UdpMediaSession(
        _FakeGateway(),  # type: ignore[arg-type]
        _grant(),
        receive,
        failures.append,
        queue_size=16,
        reorder_wait_seconds=1,
    )
    try:
        session._next_audio_sequence = 1  # noqa: SLF001
        session._reorder = {  # noqa: SLF001
            sequence: (UDP_FLAG_AUDIO, b"buffered", sequence * 960, 1)
            for sequence in range(100, 100 + UDP_JITTER_WINDOW_PACKETS)
        }
        header = UdpPacketHeader(
            flags=UDP_FLAG_AUDIO,
            media_id=session.grant.media_id,
            media_epoch=session.grant.media_epoch,
            sequence=3,
            timestamp=3 * 960,
            generation=1,
            payload_length=8,
        )

        await session._buffer_media(header, b"overflow")  # noqa: SLF001

        assert len(session._reorder) == UDP_JITTER_WINDOW_PACKETS  # noqa: SLF001
        assert session.stats.queue_dropped == 1
        assert failures == []
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_authenticated_audio_admission_drop_does_not_advance_replay_window() -> None:
    received: list[bytes] = []
    failures: list[BaseException] = []

    async def receive(payload: bytes, _timestamp: int, _generation: int) -> None:
        received.append(payload)

    grant = _grant()
    session = UdpMediaSession(
        _FakeGateway(),  # type: ignore[arg-type]
        grant,
        receive,
        failures.append,
        queue_size=16,
        reorder_wait_seconds=0.01,
    )
    try:
        session.enqueue(_uplink_packet(grant, flags=UDP_FLAG_PROBE, sequence=0, payload=b""), _SOURCE)
        await session.wait_ready(0.1)

        # Delta 3 is the final admitted slot; delta 4 is outside the canonical
        # four-slot playout window. Sequence 1025 exceeds anti-replay as well.
        session.enqueue(_uplink_packet(grant, flags=UDP_FLAG_AUDIO, sequence=1025, payload=b"replay-far"), _SOURCE)
        session.enqueue(_uplink_packet(grant, flags=UDP_FLAG_AUDIO, sequence=4, payload=b"delta-three"), _SOURCE)
        session.enqueue(_uplink_packet(grant, flags=UDP_FLAG_AUDIO, sequence=5, payload=b"delta-four"), _SOURCE)
        await _eventually(lambda: session.stats.queue_dropped == 1 and session.stats.invalid == 1)

        session.enqueue(_uplink_packet(grant, flags=UDP_FLAG_AUDIO, sequence=1, payload=b"one"), _SOURCE)
        await _eventually(lambda: received == [b"one"])

        assert session.stats.authenticated == 4
        assert failures == []
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_authenticated_control_admission_drop_does_not_advance_replay_window() -> None:
    received: list[bytes] = []
    failures: list[BaseException] = []

    async def receive(payload: bytes, _timestamp: int, _generation: int) -> None:
        received.append(payload)

    gateway = _FakeGateway()
    grant = _grant()
    session = UdpMediaSession(
        gateway,  # type: ignore[arg-type]
        grant,
        receive,
        failures.append,
        queue_size=16,
        reorder_wait_seconds=0.01,
    )
    try:
        session.enqueue(_uplink_packet(grant, flags=UDP_FLAG_PROBE, sequence=0, payload=b""), _SOURCE)
        await session.wait_ready(0.1)

        session.enqueue(_uplink_packet(grant, flags=UDP_FLAG_KEEPALIVE, sequence=4, payload=b""), _SOURCE)
        session.enqueue(_uplink_packet(grant, flags=UDP_FLAG_PROBE, sequence=5, payload=b""), _SOURCE)
        await _eventually(lambda: session.stats.queue_dropped == 1)

        session.enqueue(_uplink_packet(grant, flags=UDP_FLAG_AUDIO, sequence=1, payload=b"one"), _SOURCE)
        await _eventually(lambda: received == [b"one"])

        assert session.stats.authenticated == 4
        assert len(gateway.transport.sent) == 2
        assert failures == []
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_xiaozhi_ingress_generation_fence_discards_stale_udp_audio() -> None:
    websocket = _NoopWebSocket()
    connection = XiaozhiConnection(
        websocket,  # type: ignore[arg-type]
        _legacy_auth(),
        Settings(lab_token="test-token"),
    )
    connection._listening = True  # noqa: SLF001

    await connection._accept_udp_audio(b"generation-1", timestamp=960, generation=1)  # noqa: SLF001
    await connection._accept_udp_audio(b"future-generation", timestamp=1920, generation=2)  # noqa: SLF001
    await connection._fence_playback()  # noqa: SLF001
    await connection._accept_udp_audio(b"stale-generation", timestamp=2880, generation=1)  # noqa: SLF001
    await connection._accept_udp_audio(b"generation-2", timestamp=3840, generation=2)  # noqa: SLF001

    assert connection._input.get_nowait() == b"generation-1"  # noqa: SLF001
    assert connection._input.get_nowait() == b"generation-2"  # noqa: SLF001
    assert connection._input.empty()  # noqa: SLF001

    await connection.close()
    await connection._accept_udp_audio(b"after-close", timestamp=4800, generation=2)  # noqa: SLF001
    assert connection._input.empty()  # noqa: SLF001
    assert websocket.closed == [(1000, "normal")]


@pytest.mark.asyncio
async def test_bounded_datagram_queue_drops_burst_and_close_is_terminal_and_idempotent() -> None:
    gateway = _FakeGateway()
    grant = _grant()
    failures: list[BaseException] = []

    async def receive(payload: bytes, timestamp: int, generation: int) -> None:
        del payload, timestamp, generation

    session = UdpMediaSession(
        gateway,  # type: ignore[arg-type]
        grant,
        receive,
        failures.append,
        queue_size=2,
        reorder_wait_seconds=0.01,
    )

    # No event-loop yield occurs between enqueues, so queue saturation is deterministic.
    session.enqueue(b"first", _SOURCE)
    session.enqueue(b"second", _SOURCE)
    session.enqueue(b"dropped", _SOURCE)
    assert session.stats.received == 3
    assert session.stats.queue_dropped == 1

    await session.close()
    await session.close()
    assert gateway.removed == [(grant.media_id, session)]
    assert session._worker.done()  # noqa: SLF001
    assert session._expiry_task.done()  # noqa: SLF001

    session.enqueue(b"ignored-after-close", _SOURCE)
    assert session.stats.received == 3
    assert session.stats.queue_dropped == 1
    assert failures == []
