from __future__ import annotations

import asyncio

import pytest

from rva_desktop.errors import FreshReopenRequired, ProtocolError, TransportError
from rva_desktop.protocol import FLAG_AUDIO, FLAG_KEEPALIVE, FLAG_PROBE_ACK, MediaFrame, UdpCipher, UdpGrant
from rva_desktop.transport.udp import UdpMediaTransport

MEDIA_ID = bytes.fromhex("0123456789abcdef")
UPLINK_KEY = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
UPLINK_SALT = bytes.fromhex("a0a1a2a3a4a5a6a7")
DOWNLINK_KEY = bytes.fromhex("f0f1f2f3f4f5f6f7f8f9fafbfcfdfeff")
DOWNLINK_SALT = bytes.fromhex("b0b1b2b3b4b5b6b7")


class Clock:
    value = 10.0

    def __call__(self) -> float:
        return self.value


class FakePort:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.inbound: asyncio.Queue[bytes] = asyncio.Queue()
        self.closed = False
        self.downlink = UdpCipher(DOWNLINK_KEY, DOWNLINK_SALT)

    def send(self, data: bytes) -> None:
        self.sent.append(data)
        if len(self.sent) == 1:
            self.inbound.put_nowait(
                self.downlink.encrypt(MediaFrame(FLAG_PROBE_ACK, MEDIA_ID, 7, 0, 0, 0))
            )

    async def receive(self) -> bytes:
        return await self.inbound.get()

    def close(self) -> None:
        self.closed = True


def grant() -> UdpGrant:
    return UdpGrant(
        host="voice.test",
        port=8443,
        expires_at_ms=2_000_000,
        refresh_after_ms=1_000,
        uplink_key=UPLINK_KEY,
        uplink_salt=UPLINK_SALT,
        downlink_key=DOWNLINK_KEY,
        downlink_salt=DOWNLINK_SALT,
        probe_timeout_ms=500,
    )


def test_udp_probe_audio_and_directional_crypto() -> None:
    async def scenario() -> None:
        clock = Clock()
        port = FakePort()

        async def factory(_host: str, _port: int) -> FakePort:
            return port

        transport = UdpMediaTransport(factory=factory, monotonic=clock, wall_clock=lambda: 1.0)
        await transport.open(grant(), media_id=MEDIA_ID, media_epoch=7)
        await transport.send_audio(b"uplink", timestamp=960)
        uplink = UdpCipher(UPLINK_KEY, UPLINK_SALT).decrypt(port.sent[-1])
        assert (uplink.flags, uplink.sequence, uplink.generation, uplink.payload) == (1, 1, 0, b"uplink")

        downlink = port.downlink.encrypt(MediaFrame(FLAG_AUDIO, MEDIA_ID, 7, 1, 1920, 2, b"downlink"))
        port.inbound.put_nowait(downlink)
        received = await transport.receive_audio()
        assert received.payload == b"downlink"
        assert received.generation == 2

        port.inbound.put_nowait(downlink)
        port.inbound.put_nowait(b"invalid")
        stale_identity = port.downlink.encrypt(
            MediaFrame(FLAG_AUDIO, bytes.fromhex("fedcba9876543210"), 7, 2, 2880, 2, b"stale")
        )
        port.inbound.put_nowait(stale_identity)
        tampered = bytearray(
            port.downlink.encrypt(MediaFrame(FLAG_AUDIO, MEDIA_ID, 7, 2, 2880, 2, b"tampered"))
        )
        tampered[-1] ^= 1
        port.inbound.put_nowait(bytes(tampered))
        next_downlink = port.downlink.encrypt(MediaFrame(FLAG_AUDIO, MEDIA_ID, 7, 2, 2880, 2, b"next"))
        port.inbound.put_nowait(next_downlink)
        assert (await transport.receive_audio()).payload == b"next"
        await transport.close()
        assert port.closed

    asyncio.run(scenario())


def test_udp_keepalive_ack_advances_downlink_sequence_without_delaying_audio() -> None:
    async def scenario() -> None:
        port = FakePort()

        async def factory(_host: str, _port: int) -> FakePort:
            return port

        transport = UdpMediaTransport(factory=factory, wall_clock=lambda: 1.0)
        await transport.open(grant(), media_id=MEDIA_ID, media_epoch=7)
        await transport.send_keepalive(timestamp=960)
        keepalive = UdpCipher(UPLINK_KEY, UPLINK_SALT).decrypt(port.sent[-1])
        assert (keepalive.flags, keepalive.sequence, keepalive.timestamp) == (FLAG_KEEPALIVE, 1, 960)

        port.inbound.put_nowait(
            port.downlink.encrypt(MediaFrame(FLAG_KEEPALIVE, MEDIA_ID, 7, 1, 960, 0))
        )
        port.inbound.put_nowait(
            port.downlink.encrypt(MediaFrame(FLAG_AUDIO, MEDIA_ID, 7, 2, 1920, 2, b"audio"))
        )
        assert (await transport.receive_audio()).sequence == 2

    asyncio.run(scenario())


def test_udp_keepalive_fills_gap_and_releases_buffered_audio_immediately() -> None:
    async def scenario() -> None:
        port = FakePort()

        async def factory(_host: str, _port: int) -> FakePort:
            return port

        transport = UdpMediaTransport(factory=factory, wall_clock=lambda: 1.0)
        await transport.open(grant(), media_id=MEDIA_ID, media_epoch=7)
        port.inbound.put_nowait(
            port.downlink.encrypt(MediaFrame(FLAG_AUDIO, MEDIA_ID, 7, 2, 1920, 2, b"buffered"))
        )
        port.inbound.put_nowait(
            port.downlink.encrypt(MediaFrame(FLAG_KEEPALIVE, MEDIA_ID, 7, 1, 960, 0))
        )

        received = await asyncio.wait_for(transport.receive_audio(), timeout=0.1)
        assert received.sequence == 2
        assert received.payload == b"buffered"

    asyncio.run(scenario())


def test_udp_refresh_uses_monotonic_deadline() -> None:
    async def scenario() -> None:
        clock = Clock()
        port = FakePort()

        async def factory(_host: str, _port: int) -> FakePort:
            return port

        transport = UdpMediaTransport(factory=factory, monotonic=clock, wall_clock=lambda: 1.0)
        await transport.open(grant(), media_id=MEDIA_ID, media_epoch=7)
        clock.value += 1.01
        assert transport.refresh_due
        with pytest.raises(FreshReopenRequired):
            await transport.send_audio(b"opus", timestamp=0)

    asyncio.run(scenario())


def test_udp_probe_failure_closes_port_and_discards_keys() -> None:
    async def scenario() -> None:
        clock = Clock()
        port = FakePort()
        port.send = lambda data: port.sent.append(data)  # type: ignore[method-assign]

        async def factory(_host: str, _port: int) -> FakePort:
            return port

        transport = UdpMediaTransport(
            factory=factory,
            monotonic=clock,
            wall_clock=lambda: 1.0,
            probe_retry_seconds=0.001,
        )

        async def advance_clock() -> None:
            while not port.closed:
                clock.value += 0.1
                await asyncio.sleep(0)

        advancing = asyncio.create_task(advance_clock())
        with pytest.raises(Exception, match="udp_probe_timeout"):
            await transport.open(grant(), media_id=MEDIA_ID, media_epoch=7)
        await advancing
        assert port.closed
        assert not transport.refresh_due
        with pytest.raises(TransportError, match="transport_not_open"):
            await transport.send_keepalive(timestamp=0)

    asyncio.run(scenario())


def test_udp_generation_validation_precedes_replay_admission() -> None:
    async def scenario() -> None:
        port = FakePort()

        async def factory(_host: str, _port: int) -> FakePort:
            return port

        transport = UdpMediaTransport(factory=factory, wall_clock=lambda: 1.0)
        await transport.open(grant(), media_id=MEDIA_ID, media_epoch=7)
        validations = 0

        def validate_generation(_frame: MediaFrame) -> bool:
            nonlocal validations
            validations += 1
            if validations == 1:
                raise ProtocolError("unknown_media_generation")
            return True

        transport.set_media_validator(validate_generation)
        datagram = port.downlink.encrypt(
            MediaFrame(FLAG_AUDIO, MEDIA_ID, 7, 1, 960, 3, b"admitted-after-control")
        )
        # A packet rejected because its response control has not arrived must not
        # poison replay admission when the same authenticated packet is retried.
        port.inbound.put_nowait(datagram)
        port.inbound.put_nowait(datagram)

        received = await asyncio.wait_for(transport.receive_audio(), timeout=1)
        assert received.payload == b"admitted-after-control"
        assert validations == 2

    asyncio.run(scenario())


def test_udp_probe_ignores_authenticated_noncanonical_ack_without_poisoning_replay() -> None:
    async def scenario() -> None:
        class RetryAckPort(FakePort):
            def send(self, data: bytes) -> None:
                self.sent.append(data)
                ack_sequence = 500 if len(self.sent) == 1 else 0
                self.inbound.put_nowait(
                    self.downlink.encrypt(MediaFrame(FLAG_PROBE_ACK, MEDIA_ID, 7, ack_sequence, 0, 0))
                )

        port = RetryAckPort()

        async def factory(_host: str, _port: int) -> RetryAckPort:
            return port

        transport = UdpMediaTransport(
            factory=factory,
            wall_clock=lambda: 1.0,
            probe_retry_seconds=0.001,
        )
        await transport.open(grant(), media_id=MEDIA_ID, media_epoch=7)
        assert len(port.sent) == 2
        port.inbound.put_nowait(
            port.downlink.encrypt(MediaFrame(FLAG_AUDIO, MEDIA_ID, 7, 1, 960, 2, b"audio"))
        )
        assert (await transport.receive_audio()).sequence == 1

    asyncio.run(scenario())


def test_udp_gap_deadline_is_not_extended_by_invalid_datagrams() -> None:
    async def scenario() -> None:
        port = FakePort()

        async def factory(_host: str, _port: int) -> FakePort:
            return port

        transport = UdpMediaTransport(
            factory=factory,
            wall_clock=lambda: 1.0,
            max_media_age_seconds=0.03,
        )
        await transport.open(grant(), media_id=MEDIA_ID, media_epoch=7)
        port.inbound.put_nowait(
            port.downlink.encrypt(MediaFrame(FLAG_AUDIO, MEDIA_ID, 7, 2, 1920, 2, b"after-gap"))
        )

        async def invalid_flood() -> None:
            for _ in range(30):
                port.inbound.put_nowait(b"invalid")
                await asyncio.sleep(0.005)

        flood = asyncio.create_task(invalid_flood())
        started = asyncio.get_running_loop().time()
        received = await asyncio.wait_for(transport.receive_audio(), timeout=0.1)
        elapsed = asyncio.get_running_loop().time() - started
        flood.cancel()
        await asyncio.gather(flood, return_exceptions=True)
        assert received.sequence == 2
        assert elapsed < 0.08

    asyncio.run(scenario())


def test_udp_pending_control_deadline_is_not_extended_by_invalid_datagrams() -> None:
    async def scenario() -> None:
        port = FakePort()

        async def factory(_host: str, _port: int) -> FakePort:
            return port

        transport = UdpMediaTransport(
            factory=factory,
            wall_clock=lambda: 1.0,
            max_media_age_seconds=0.03,
        )
        await transport.open(grant(), media_id=MEDIA_ID, media_epoch=7)

        def unknown_generation(_frame: MediaFrame) -> bool:
            raise ProtocolError("unknown_media_generation")

        transport.set_media_validator(unknown_generation)
        port.inbound.put_nowait(
            port.downlink.encrypt(MediaFrame(FLAG_AUDIO, MEDIA_ID, 7, 1, 960, 3, b"early-media"))
        )

        async def invalid_flood() -> None:
            for _ in range(30):
                port.inbound.put_nowait(b"invalid")
                await asyncio.sleep(0.005)

        flood = asyncio.create_task(invalid_flood())
        with pytest.raises(FreshReopenRequired, match="waited too long"):
            await asyncio.wait_for(transport.receive_audio(), timeout=0.1)
        flood.cancel()
        await asyncio.gather(flood, return_exceptions=True)

    asyncio.run(scenario())
