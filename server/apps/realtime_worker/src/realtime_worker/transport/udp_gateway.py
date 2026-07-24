"""Control-protocol-neutral authenticated UDP media gateway."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .udp_wire import (
    UDP_FLAG_AUDIO,
    UDP_FLAG_KEEPALIVE,
    UDP_FLAG_PROBE,
    UDP_FLAG_PROBE_ACK,
    UDP_HEADER_BYTES,
    UDP_JITTER_WINDOW_PACKETS,
    UDP_KEY_BYTES,
    UDP_MAX_DATAGRAM_BYTES,
    UDP_MAX_PAYLOAD_BYTES,
    UDP_SALT_BYTES,
    UDP_TAG_BYTES,
    ReplayWindow,
    UdpPacketHeader,
)

logger = logging.getLogger(__name__)

AudioReceiver = Callable[[bytes, int, int], Awaitable[None]]
FailureReceiver = Callable[[BaseException], None]


class UdpMediaError(RuntimeError):
    """The UDP media session cannot continue safely."""


class UdpProbeTimeoutError(UdpMediaError):
    """The client did not prove the UDP media path within the bounded probe window."""


@dataclass(frozen=True, slots=True)
class UdpGrant:
    media_id: bytes
    media_epoch: int
    uplink_key: bytes
    uplink_salt: bytes
    downlink_key: bytes
    downlink_salt: bytes
    host: str
    port: int
    expires_at: int
    probe_timeout_ms: int

    def as_control_payload(self) -> dict[str, object]:
        return {
            "server": self.host,
            "port": self.port,
            "media_id": self.media_id.hex(),
            "media_epoch": self.media_epoch,
            "uplink_key": base64.b64encode(self.uplink_key).decode("ascii"),
            "uplink_salt": base64.b64encode(self.uplink_salt).decode("ascii"),
            "downlink_key": base64.b64encode(self.downlink_key).decode("ascii"),
            "downlink_salt": base64.b64encode(self.downlink_salt).decode("ascii"),
            "expires_at": self.expires_at,
            "probe_timeout_ms": self.probe_timeout_ms,
            "header_bytes": UDP_HEADER_BYTES,
            "tag_bytes": UDP_TAG_BYTES,
            "max_datagram_bytes": UDP_MAX_DATAGRAM_BYTES,
            "max_payload_bytes": UDP_MAX_PAYLOAD_BYTES,
        }


@dataclass(slots=True)
class UdpMediaStats:
    received: int = 0
    authenticated: int = 0
    invalid: int = 0
    replayed: int = 0
    wrong_source: int = 0
    queue_dropped: int = 0
    sent: int = 0
    lost: int = 0
    reordered: int = 0


class _GatewayProtocol(asyncio.DatagramProtocol):
    def __init__(self, gateway: UdpMediaGateway, generation: int) -> None:
        self._gateway = gateway
        self._generation = generation

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._gateway.transport_started(self, self._generation, transport)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._gateway.route_datagram(self, self._generation, data, addr)

    def error_received(self, exc: Exception) -> None:
        self._gateway.transport_error(self, self._generation, exc)

    def connection_lost(self, exc: Exception | None) -> None:
        self._gateway.transport_lost(
            self,
            self._generation,
            exc or UdpMediaError("UDP gateway closed"),
        )


class UdpMediaSession:
    def __init__(
        self,
        gateway: UdpMediaGateway,
        grant: UdpGrant,
        receive_audio: AudioReceiver,
        report_failure: FailureReceiver,
        *,
        queue_size: int,
        reorder_wait_seconds: float,
    ) -> None:
        self._gateway = gateway
        self.grant = grant
        self._receive_audio = receive_audio
        self._report_failure = report_failure
        self._queue: asyncio.Queue[tuple[bytes, tuple[str, int]] | None] = asyncio.Queue(queue_size)
        self._ready = asyncio.Event()
        self._closed = False
        self._expired = False
        self._source: tuple[str, int] | None = None
        self._uplink = AESGCM(grant.uplink_key)
        self._downlink = AESGCM(grant.downlink_key)
        self._replay = ReplayWindow()
        self._downlink_sequence = 0
        self._next_audio_sequence: int | None = None
        self._reorder_wait_seconds = reorder_wait_seconds
        self._reorder: dict[int, tuple[int, bytes, int, int]] = {}
        self._reorder_timer: asyncio.Task[None] | None = None
        self._worker = asyncio.create_task(self._run(), name=f"udp-media-{grant.media_id.hex()}")
        self._expiry_task = asyncio.create_task(self._expire(), name=f"udp-expiry-{grant.media_id.hex()}")
        self.stats = UdpMediaStats()

    def enqueue(self, data: bytes, addr: tuple[str, int]) -> None:
        if self._closed:
            return
        self.stats.received += 1
        try:
            self._queue.put_nowait((data, addr))
        except asyncio.QueueFull:
            self.stats.queue_dropped += 1

    async def wait_ready(self, timeout: float) -> None:
        started_at = time.monotonic()
        logger.info(
            "udp_wait_ready_started media_id=%s media_epoch=%d timeout_ms=%d",
            self.grant.media_id.hex(),
            self.grant.media_epoch,
            int(timeout * 1000),
        )
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except TimeoutError as exc:
            logger.warning(
                "udp_wait_ready_failed reason=udp_probe_timeout media_id=%s media_epoch=%d timeout_ms=%d "
                "received=%d authenticated=%d invalid=%d replayed=%d wrong_source=%d queue_dropped=%d",
                self.grant.media_id.hex(),
                self.grant.media_epoch,
                int(timeout * 1000),
                self.stats.received,
                self.stats.authenticated,
                self.stats.invalid,
                self.stats.replayed,
                self.stats.wrong_source,
                self.stats.queue_dropped,
            )
            raise UdpProbeTimeoutError("UDP media probe timed out") from exc
        logger.info(
            "udp_wait_ready_completed media_id=%s media_epoch=%d elapsed_ms=%d authenticated=%d invalid=%d",
            self.grant.media_id.hex(),
            self.grant.media_epoch,
            int((time.monotonic() - started_at) * 1000),
            self.stats.authenticated,
            self.stats.invalid,
        )

    async def send_audio(self, payload: bytes, *, timestamp: int, generation: int) -> int:
        if self._expired or time.time() >= self.grant.expires_at:
            raise UdpMediaError("UDP media session expired")
        if self._closed or self._source is None or not self._ready.is_set():
            raise UdpMediaError("UDP media path is not ready")
        return await self._send(UDP_FLAG_AUDIO, payload, timestamp=timestamp, generation=generation)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._gateway.remove(self.grant.media_id, self)
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(None)
        self._worker.cancel()
        self._expiry_task.cancel()
        if self._reorder_timer is not None:
            self._reorder_timer.cancel()
        await asyncio.gather(
            self._worker,
            self._expiry_task,
            *(task for task in (self._reorder_timer,) if task is not None),
            return_exceptions=True,
        )

    async def _expire(self) -> None:
        try:
            await asyncio.sleep(max(0.0, self.grant.expires_at - time.time()))
            if not self._closed:
                self._expired = True
                self._gateway.remove(self.grant.media_id, self)
                self._worker.cancel()
                if self._reorder_timer is not None:
                    self._reorder_timer.cancel()
                self._report_failure(UdpMediaError("UDP media session expired"))
        except asyncio.CancelledError:
            raise

    async def _run(self) -> None:
        try:
            while True:
                queued = await self._queue.get()
                if queued is None:
                    return
                data, addr = queued
                await self._handle(data, addr)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._report_failure(exc)

    async def _handle(self, data: bytes, addr: tuple[str, int]) -> None:
        if len(data) > UDP_MAX_DATAGRAM_BYTES or self._expired or time.time() >= self.grant.expires_at:
            self.stats.invalid += 1
            return
        try:
            header, encrypted = UdpPacketHeader.decode(data)
        except ValueError:
            self.stats.invalid += 1
            return
        if header.media_id != self.grant.media_id or header.media_epoch != self.grant.media_epoch:
            self.stats.invalid += 1
            return
        if header.flags not in {UDP_FLAG_AUDIO, UDP_FLAG_PROBE, UDP_FLAG_KEEPALIVE}:
            self.stats.invalid += 1
            return
        if not self._replay.can_accept(header.sequence):
            if self._replay.exceeds_forward_window(header.sequence):
                self.stats.invalid += 1
            else:
                self.stats.replayed += 1
            return
        try:
            payload = self._uplink.decrypt(
                self.grant.uplink_salt + header.sequence.to_bytes(4, "big"),
                encrypted,
                data[:UDP_HEADER_BYTES],
            )
        except Exception:
            self.stats.invalid += 1
            return
        self.stats.authenticated += 1
        if self._source is not None and addr != self._source:
            self.stats.wrong_source += 1
            return
        if header.flags == UDP_FLAG_PROBE:
            if payload:
                self.stats.invalid += 1
                return
            if self._source is None and header.sequence != 0:
                self.stats.invalid += 1
                return
            expected = self._next_audio_sequence
            if expected is not None:
                if header.sequence < expected:
                    self.stats.replayed += 1
                    return
                if header.sequence >= expected + UDP_JITTER_WINDOW_PACKETS or (
                    header.sequence != expected and len(self._reorder) >= UDP_JITTER_WINDOW_PACKETS
                ):
                    self.stats.queue_dropped += 1
                    return
            self._replay.commit(header.sequence)
            self._source = addr
            await self._send(UDP_FLAG_PROBE_ACK, b"", timestamp=0, generation=0)
            if expected is None:
                self._next_audio_sequence = header.sequence
            await self._buffer_media(header, b"")
            self._ready.set()
            return
        if self._source is None or not self._ready.is_set():
            self.stats.invalid += 1
            return
        if header.flags == UDP_FLAG_KEEPALIVE and payload:
            self.stats.invalid += 1
            return
        if header.flags == UDP_FLAG_AUDIO and not payload:
            self.stats.invalid += 1
            return
        expected = self._next_audio_sequence
        if expected is None:
            self.stats.invalid += 1
            return
        if header.sequence < expected:
            self.stats.replayed += 1
            return
        if (
            header.sequence >= expected + UDP_JITTER_WINDOW_PACKETS
            or (header.sequence != expected and len(self._reorder) >= UDP_JITTER_WINDOW_PACKETS)
        ):
            self.stats.queue_dropped += 1
            return
        self._replay.commit(header.sequence)
        await self._buffer_media(header, payload)
        if header.flags == UDP_FLAG_KEEPALIVE:
            await self._send(
                UDP_FLAG_KEEPALIVE,
                b"",
                timestamp=header.timestamp,
                generation=header.generation,
            )

    async def _buffer_media(self, header: UdpPacketHeader, payload: bytes) -> None:
        expected = self._next_audio_sequence
        if expected is None:
            self.stats.invalid += 1
            return
        if header.sequence < expected:
            self.stats.replayed += 1
            return
        if header.sequence >= expected + UDP_JITTER_WINDOW_PACKETS:
            self.stats.queue_dropped += 1
            return
        if len(self._reorder) >= UDP_JITTER_WINDOW_PACKETS:
            if header.sequence != expected:
                self.stats.queue_dropped += 1
                return
            if header.flags == UDP_FLAG_AUDIO:
                await self._receive_audio(payload, header.timestamp, header.generation)
            self._next_audio_sequence = expected + 1
            await self._drain_reorder()
            return
        if header.sequence != expected:
            self.stats.reordered += 1
        self._reorder[header.sequence] = (
            header.flags,
            payload,
            header.timestamp,
            header.generation,
        )
        await self._drain_reorder()

    async def _drain_reorder(self) -> None:
        expected = self._next_audio_sequence
        while expected is not None and expected in self._reorder:
            flags, payload, timestamp, generation = self._reorder.pop(expected)
            if flags == UDP_FLAG_AUDIO:
                await self._receive_audio(payload, timestamp, generation)
            expected += 1
            self._next_audio_sequence = expected
        if not self._reorder and self._reorder_timer is not None:
            self._reorder_timer.cancel()
            self._reorder_timer = None
        elif self._reorder and self._reorder_timer is None:
            self._reorder_timer = asyncio.create_task(
                self._expire_reorder_gap(), name=f"udp-reorder-{self.grant.media_id.hex()}"
            )

    async def _expire_reorder_gap(self) -> None:
        try:
            await asyncio.sleep(self._reorder_wait_seconds)
            if self._closed or not self._reorder:
                return
            first = min(self._reorder)
            expected = self._next_audio_sequence
            if expected is not None and first > expected:
                self.stats.lost += first - expected
                self._next_audio_sequence = first
            self._reorder_timer = None
            await self._drain_reorder()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._report_failure(exc)

    async def _send(self, flags: int, payload: bytes, *, timestamp: int, generation: int) -> int:
        if self._expired or time.time() >= self.grant.expires_at:
            raise UdpMediaError("UDP media session expired")
        if len(payload) > UDP_MAX_PAYLOAD_BYTES:
            raise UdpMediaError("UDP payload exceeds profile limit")
        source = self._source
        transport = self._gateway.transport
        if source is None or transport is None:
            raise UdpMediaError("UDP transport is unavailable")
        sequence = self._downlink_sequence
        if sequence >= 0xFFFFFFFF:
            raise UdpMediaError("UDP sequence exhausted")
        self._downlink_sequence += 1
        header = UdpPacketHeader(
            flags,
            self.grant.media_id,
            self.grant.media_epoch,
            sequence,
            timestamp & 0xFFFFFFFF,
            generation,
            len(payload),
        )
        aad = header.encode()
        encrypted = self._downlink.encrypt(self.grant.downlink_salt + sequence.to_bytes(4, "big"), payload, aad)
        datagram = aad + encrypted
        if len(datagram) > UDP_MAX_DATAGRAM_BYTES:
            raise UdpMediaError("UDP datagram exceeds conservative MTU")
        transport.sendto(datagram, source)
        self.stats.sent += 1
        return sequence


class UdpMediaGateway:
    """One process-owned UDP socket with bounded opaque session routing."""

    def __init__(
        self,
        *,
        bind_host: str,
        bind_port: int,
        advertised_host: str,
        lifetime_seconds: int,
        probe_timeout_seconds: float,
        queue_size: int,
        reorder_wait_seconds: float,
        advertised_port: int = 0,
    ) -> None:
        self._bind_host = bind_host
        self._bind_port = bind_port
        self._advertised_host = advertised_host
        self._advertised_port = advertised_port
        self._lifetime_seconds = lifetime_seconds
        self._probe_timeout_seconds = probe_timeout_seconds
        self._queue_size = queue_size
        self._reorder_wait_seconds = reorder_wait_seconds
        self._transport: asyncio.DatagramTransport | None = None
        self._protocol: _GatewayProtocol | None = None
        self._transport_generation = 0
        self._sessions: dict[bytes, UdpMediaSession] = {}
        self._failure: BaseException | None = None
        self._closing = False

    @property
    def transport(self) -> asyncio.DatagramTransport | None:
        return self._transport

    @property
    def is_ready(self) -> bool:
        return self._transport is not None and self._failure is None and not self._closing

    @property
    def session_count(self) -> int:
        return len(self._sessions)

    @property
    def local_port(self) -> int:
        if self._transport is None:
            return self._bind_port
        return int(self._transport.get_extra_info("sockname")[1])

    async def start(self) -> None:
        if self.is_ready:
            return
        self._closing = False
        self._failure = None
        loop = asyncio.get_running_loop()
        self._transport_generation += 1
        generation = self._transport_generation
        protocol = _GatewayProtocol(self, generation)
        self._protocol = protocol
        try:
            await loop.create_datagram_endpoint(
                lambda: protocol,
                local_addr=(self._bind_host, self._bind_port),
            )
        except BaseException as exc:
            if self._is_current_transport(protocol, generation):
                self._protocol = None
                self._transport = None
                self._failure = exc
            raise

    async def close(self) -> None:
        self._closing = True
        sessions, self._sessions = tuple(self._sessions.values()), {}
        await asyncio.gather(*(session.close() for session in sessions), return_exceptions=True)
        self._protocol = None
        transport, self._transport = self._transport, None
        if transport is not None:
            transport.close()

    def create_session(self, receive_audio: AudioReceiver, report_failure: FailureReceiver) -> UdpMediaSession:
        if not self.is_ready:
            raise UdpMediaError("UDP gateway is unavailable")
        while (media_id := secrets.token_bytes(8)) in self._sessions:
            pass
        grant = UdpGrant(
            media_id=media_id,
            media_epoch=secrets.randbits(32) or 1,
            uplink_key=secrets.token_bytes(UDP_KEY_BYTES),
            uplink_salt=secrets.token_bytes(UDP_SALT_BYTES),
            downlink_key=secrets.token_bytes(UDP_KEY_BYTES),
            downlink_salt=secrets.token_bytes(UDP_SALT_BYTES),
            host=self._advertised_host,
            port=self._advertised_port or self.local_port,
            expires_at=int(time.time()) + self._lifetime_seconds,
            probe_timeout_ms=int(self._probe_timeout_seconds * 1000),
        )
        session = UdpMediaSession(
            self,
            grant,
            receive_audio,
            report_failure,
            queue_size=self._queue_size,
            reorder_wait_seconds=self._reorder_wait_seconds,
        )
        self._sessions[media_id] = session
        return session

    def transport_started(
        self,
        protocol: _GatewayProtocol,
        generation: int,
        transport: asyncio.BaseTransport,
    ) -> None:
        if self._closing or not self._is_current_transport(protocol, generation):
            transport.close()
            return
        self._transport = transport  # type: ignore[assignment]

    def route_datagram(
        self,
        protocol: _GatewayProtocol,
        generation: int,
        data: bytes,
        addr: tuple[str, int],
    ) -> None:
        if not self._is_current_transport(protocol, generation):
            return
        if len(data) < UDP_HEADER_BYTES:
            return
        media_id = data[4:12]
        session = self._sessions.get(media_id)
        if session is not None:
            session.enqueue(data, addr)

    def transport_error(
        self,
        protocol: _GatewayProtocol,
        generation: int,
        exc: BaseException,
    ) -> None:
        if self._closing or not self._is_current_transport(protocol, generation):
            return
        logger.warning(
            "UDP gateway observed recoverable UDP socket error generation=%d error_type=%s",
            generation,
            type(exc).__name__,
        )

    def transport_lost(
        self,
        protocol: _GatewayProtocol,
        generation: int,
        exc: BaseException,
    ) -> None:
        self._fail_transport(protocol, generation, exc, close_transport=False)

    def transport_failed(self, exc: BaseException) -> None:
        protocol = self._protocol
        if protocol is None:
            return
        self._fail_transport(
            protocol,
            self._transport_generation,
            exc,
            close_transport=True,
        )

    def _fail_transport(
        self,
        protocol: _GatewayProtocol,
        generation: int,
        exc: BaseException,
        *,
        close_transport: bool,
    ) -> None:
        if self._closing or self._failure is not None or not self._is_current_transport(protocol, generation):
            return
        self._failure = exc
        self._protocol = None
        transport, self._transport = self._transport, None
        if close_transport and transport is not None:
            transport.close()
        for session in tuple(self._sessions.values()):
            session._report_failure(exc)  # noqa: SLF001

    def _is_current_transport(self, protocol: _GatewayProtocol, generation: int) -> bool:
        return self._protocol is protocol and self._transport_generation == generation

    def remove(self, media_id: bytes, session: UdpMediaSession) -> None:
        if self._sessions.get(media_id) is session:
            self._sessions.pop(media_id, None)
