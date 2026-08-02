from __future__ import annotations

import asyncio
import logging
import time

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from realtime_worker.transport.udp_gateway import (
    UdpGrant,
    UdpGrantExpiredError,
    UdpMediaGateway,
    UdpMediaSession,
)
from realtime_worker.transport.udp_wire import (
    UDP_FLAG_AUDIO,
    UDP_FLAG_KEEPALIVE,
    UDP_FLAG_PROBE,
    UDP_FLAG_PROBE_ACK,
    UDP_HEADER_BYTES,
    UdpPacketHeader,
)


class CapturingTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []

    def sendto(self, datagram: bytes, addr: tuple[str, int]) -> None:
        self.sent.append((datagram, addr))


class FakeGateway:
    def __init__(self) -> None:
        self.transport = CapturingTransport()

    def remove(self, _media_id: bytes, _session: UdpMediaSession) -> None:
        pass


@pytest.mark.asyncio
async def test_expiry_reports_planned_grant_expiration() -> None:
    gateway = FakeGateway()
    failures: list[BaseException] = []
    failure_reported = asyncio.Event()
    grant = UdpGrant(
        media_id=bytes.fromhex("0102030405060708"),
        media_epoch=7,
        uplink_key=bytes.fromhex("00112233445566778899aabbccddeeff"),
        uplink_salt=bytes.fromhex("0102030405060708"),
        downlink_key=bytes.fromhex("ffeeddccbbaa99887766554433221100"),
        downlink_salt=bytes.fromhex("0807060504030201"),
        host="127.0.0.1",
        port=8093,
        expires_at=int(time.time()),
        probe_timeout_ms=500,
    )

    async def receive_audio(_payload: bytes, _timestamp: int, _generation: int) -> None:
        pass

    def report_failure(error: BaseException) -> None:
        failures.append(error)
        failure_reported.set()

    session = UdpMediaSession(
        gateway,  # type: ignore[arg-type]
        grant,
        receive_audio,
        report_failure,
        queue_size=2,
        reorder_wait_seconds=0.01,
    )
    try:
        await asyncio.wait_for(failure_reported.wait(), timeout=0.5)
        assert len(failures) == 1
        assert isinstance(failures[0], UdpGrantExpiredError)
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_probe_ack_uses_zero_generation_required_by_wire(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="realtime_worker.transport.udp_gateway")
    gateway = FakeGateway()
    grant = UdpGrant(
        media_id=bytes.fromhex("0102030405060708"),
        media_epoch=7,
        uplink_key=bytes.fromhex("00112233445566778899aabbccddeeff"),
        uplink_salt=bytes.fromhex("0102030405060708"),
        downlink_key=bytes.fromhex("ffeeddccbbaa99887766554433221100"),
        downlink_salt=bytes.fromhex("0807060504030201"),
        host="127.0.0.1",
        port=8093,
        expires_at=int(time.time()) + 60,
        probe_timeout_ms=500,
    )

    received_audio: list[tuple[bytes, int, int]] = []
    async def receive_audio(payload: bytes, timestamp: int, generation: int) -> None:
        received_audio.append((payload, timestamp, generation))

    session = UdpMediaSession(
        gateway,  # type: ignore[arg-type]
        grant,
        receive_audio,
        lambda _exc: None,
        queue_size=2,
        reorder_wait_seconds=1.0,
    )
    source = ("192.0.2.10", 45678)
    probe_header = UdpPacketHeader(
        flags=UDP_FLAG_PROBE,
        media_id=grant.media_id,
        media_epoch=grant.media_epoch,
        sequence=0,
        timestamp=0,
        generation=0,
        payload_length=0,
    )
    aad = probe_header.encode()
    encrypted = AESGCM(grant.uplink_key).encrypt(grant.uplink_salt + bytes(4), b"", aad)

    try:
        session.enqueue(aad + encrypted, source)
        await session.wait_ready(0.5)

        assert len(gateway.transport.sent) == 1
        datagram, destination = gateway.transport.sent[0]
        ack, ciphertext = UdpPacketHeader.decode(datagram)
        payload = AESGCM(grant.downlink_key).decrypt(
            grant.downlink_salt + ack.sequence.to_bytes(4, "big"),
            ciphertext,
            datagram[:UDP_HEADER_BYTES],
        )
        assert destination == source
        assert ack.flags == UDP_FLAG_PROBE_ACK
        assert ack.timestamp == 0
        assert ack.generation == 0
        assert payload == b""
        assert session.stats.received == 1
        assert session.stats.media_identity_matched == 1
        assert session.stats.authentication_attempted == 1
        assert session.stats.authenticated == 1
        assert session.stats.source_pinned == 1
        assert session.stats.probe_ack_sent == 1
        assert "datagram_arrived=1" in caplog.text
        assert "media_identity_matched=1" in caplog.text
        assert "authentication_attempted=1" in caplog.text
        assert "source_pinned=1" in caplog.text
        assert "probe_ack_sent=1" in caplog.text

        # The endpoint retransmits the exact sequence-zero probe when the first
        # ACK is lost. Before audio starts, this authenticated replay is
        # idempotent and must produce another ACK without advancing uplink state.
        session.enqueue(aad + encrypted, ("192.0.2.11", 45678))
        await asyncio.sleep(0.01)
        assert len(gateway.transport.sent) == 1
        await asyncio.sleep(0.25)
        session.enqueue(aad + encrypted, source)
        async with asyncio.timeout(0.5):
            while len(gateway.transport.sent) < 2:
                await asyncio.sleep(0)
        retry_datagram, retry_destination = gateway.transport.sent[1]
        retry_ack, retry_ciphertext = UdpPacketHeader.decode(retry_datagram)
        retry_payload = AESGCM(grant.downlink_key).decrypt(
            grant.downlink_salt + bytes(4),
            retry_ciphertext,
            retry_datagram[:UDP_HEADER_BYTES],
        )
        assert retry_destination == source
        assert retry_ack.flags == UDP_FLAG_PROBE_ACK
        assert retry_ack.sequence == 0
        assert retry_ack.timestamp == 0
        assert retry_ack.generation == 0
        assert retry_payload == b""
        assert retry_datagram == datagram
        assert session.stats.authenticated == 2
        assert session.stats.replayed == 2
        assert session.stats.probe_ack_sent == 2

        downlink_sequence = await session.send_audio(b"\xf8", timestamp=960, generation=1)
        assert downlink_sequence == 1
        downlink_header, _ = UdpPacketHeader.decode(gateway.transport.sent[2][0])
        assert downlink_header.sequence == 1

        # Any admitted post-probe uplink closes the re-ACK window, even when a
        # gap keeps the playout cursor at sequence one.
        reordered_payload = b"\xf8\x02"
        reordered_header = UdpPacketHeader(
            flags=UDP_FLAG_AUDIO,
            media_id=grant.media_id,
            media_epoch=grant.media_epoch,
            sequence=2,
            timestamp=1920,
            generation=0,
            payload_length=len(reordered_payload),
        )
        reordered_aad = reordered_header.encode()
        reordered_encrypted = AESGCM(grant.uplink_key).encrypt(
            grant.uplink_salt + (2).to_bytes(4, "big"), reordered_payload, reordered_aad
        )
        session.enqueue(reordered_aad + reordered_encrypted, source)
        async with asyncio.timeout(0.5):
            while session.stats.authenticated < 3:
                await asyncio.sleep(0)
        assert received_audio == []

        await asyncio.sleep(0.25)
        session.enqueue(aad + encrypted, source)
        async with asyncio.timeout(0.5):
            while session.stats.replayed < 3:
                await asyncio.sleep(0)
        assert len(gateway.transport.sent) == 3

        audio_payload = b"\xf8\xff\xfe"
        audio_header = UdpPacketHeader(
            flags=UDP_FLAG_AUDIO,
            media_id=grant.media_id,
            media_epoch=grant.media_epoch,
            sequence=1,
            timestamp=960,
            generation=0,
            payload_length=len(audio_payload),
        )
        audio_aad = audio_header.encode()
        audio_encrypted = AESGCM(grant.uplink_key).encrypt(
            grant.uplink_salt + (1).to_bytes(4, "big"), audio_payload, audio_aad
        )
        session.enqueue(audio_aad + audio_encrypted, source)
        async with asyncio.timeout(0.5):
            while len(received_audio) < 2:
                await asyncio.sleep(0)
        assert received_audio == [(audio_payload, 960, 0), (reordered_payload, 1920, 0)]
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_authenticated_forward_gap_resyncs_uplink_media_cursor() -> None:
    gateway = FakeGateway()
    grant = UdpGrant(
        media_id=bytes.fromhex("0102030405060708"),
        media_epoch=7,
        uplink_key=bytes.fromhex("00112233445566778899aabbccddeeff"),
        uplink_salt=bytes.fromhex("0102030405060708"),
        downlink_key=bytes.fromhex("ffeeddccbbaa99887766554433221100"),
        downlink_salt=bytes.fromhex("0807060504030201"),
        host="127.0.0.1",
        port=8093,
        expires_at=int(time.time()) + 60,
        probe_timeout_ms=500,
    )
    received_audio: list[tuple[bytes, int, int]] = []

    async def receive_audio(payload: bytes, timestamp: int, generation: int) -> None:
        received_audio.append((payload, timestamp, generation))

    session = UdpMediaSession(
        gateway,  # type: ignore[arg-type]
        grant,
        receive_audio,
        lambda _exc: None,
        queue_size=8,
        reorder_wait_seconds=0.01,
    )
    source = ("192.0.2.10", 45678)

    def datagram(flags: int, sequence: int, payload: bytes, timestamp: int = 0) -> bytes:
        header = UdpPacketHeader(
            flags=flags,
            media_id=grant.media_id,
            media_epoch=grant.media_epoch,
            sequence=sequence,
            timestamp=timestamp,
            generation=0,
            payload_length=len(payload),
        )
        aad = header.encode()
        encrypted = AESGCM(grant.uplink_key).encrypt(
            grant.uplink_salt + sequence.to_bytes(4, "big"), payload, aad
        )
        return aad + encrypted

    try:
        session.enqueue(datagram(UDP_FLAG_PROBE, 0, b""), source)
        await session.wait_ready(0.5)
        session.enqueue(datagram(UDP_FLAG_AUDIO, 1, b"one", 960), source)
        async with asyncio.timeout(0.5):
            while len(received_audio) < 1:
                await asyncio.sleep(0)

        session.enqueue(datagram(UDP_FLAG_AUDIO, 6, b"six", 5760), source)
        async with asyncio.timeout(0.5):
            while len(received_audio) < 2:
                await asyncio.sleep(0)

        session.enqueue(datagram(UDP_FLAG_KEEPALIVE, 11, b""), source)
        session.enqueue(datagram(UDP_FLAG_AUDIO, 12, b"twelve", 11520), source)
        async with asyncio.timeout(0.5):
            while len(received_audio) < 3:
                await asyncio.sleep(0)

        assert received_audio == [(b"one", 960, 0), (b"six", 5760, 0), (b"twelve", 11520, 0)]
        assert session.stats.resync_total == 2
        assert session.stats.skipped_sequences_total == 8
        assert session.stats.lost == 8
    finally:
        await session.close()


def test_gateway_rate_limits_unknown_datagram_aggregate_without_peer_or_media_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gateway = UdpMediaGateway(
        bind_host="127.0.0.1",
        bind_port=8093,
        advertised_host="voice.example.test",
        lifetime_seconds=60,
        probe_timeout_seconds=0.5,
        queue_size=2,
        reorder_wait_seconds=0.01,
    )
    protocol = object()
    gateway._protocol = protocol  # type: ignore[assignment]  # noqa: SLF001
    gateway._transport_generation = 1  # noqa: SLF001
    peer = ("203.0.113.9", 45678)
    unknown_media_id = bytes.fromhex("0102030405060708")
    unknown = bytes(4) + unknown_media_id + bytes(UDP_HEADER_BYTES - 12)
    caplog.set_level(logging.INFO, logger="realtime_worker.transport.udp_gateway")

    gateway.route_datagram(protocol, 1, b"short", peer)  # type: ignore[arg-type]
    for _ in range(3):
        gateway.route_datagram(protocol, 1, unknown, peer)  # type: ignore[arg-type]

    assert gateway.stats.datagram_arrived == 4
    assert gateway.stats.undersized == 1
    assert gateway.stats.media_identity_unknown == 3
    assert gateway.stats.media_identity_matched == 0
    aggregate_logs = [record.message for record in caplog.records if "udp_gateway_unknown_datagrams" in record.message]
    assert len(aggregate_logs) == 1
    assert "transport_generation=1" in aggregate_logs[0]
    assert "203.0.113.9" not in caplog.text
    assert unknown_media_id.hex() not in caplog.text
