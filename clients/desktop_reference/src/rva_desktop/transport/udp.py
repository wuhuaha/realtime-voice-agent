from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
from typing import Protocol

from ..errors import FreshReopenRequired, ProtocolError, TransportError
from ..protocol import (
    FLAG_AUDIO,
    FLAG_KEEPALIVE,
    FLAG_PROBE,
    FLAG_PROBE_ACK,
    MediaFrame,
    ReplayWindow,
    UdpCipher,
    UdpGrant,
)
from ..trace import NullTrace, TraceSink


class DatagramPort(Protocol):
    def send(self, data: bytes) -> None: ...

    async def receive(self) -> bytes: ...

    def close(self) -> None: ...


DatagramFactory = Callable[[str, int], asyncio.Future[DatagramPort]]


class UdpMediaTransport:
    def __init__(
        self,
        *,
        factory: Callable[[str, int], object] | None = None,
        monotonic=time.monotonic,
        wall_clock=time.time,
        trace: TraceSink | None = None,
        max_media_age_seconds: float = 0.12,
        probe_retry_seconds: float = 0.2,
    ) -> None:
        self._factory = factory or _create_port
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._trace = trace or NullTrace()
        self._max_media_age = max_media_age_seconds
        self._probe_retry = probe_retry_seconds
        self._port: DatagramPort | None = None
        self._grant: UdpGrant | None = None
        self._media_id = b""
        self._media_epoch = 0
        self._uplink: UdpCipher | None = None
        self._downlink: UdpCipher | None = None
        self._replay = ReplayWindow()
        self._next_uplink = 0
        self._expected_downlink = 1
        self._reorder: dict[int, MediaFrame] = {}
        self._reorder_deadline_at: float | None = None
        self._non_audio_sequences: set[int] = set()
        self._ready: deque[MediaFrame] = deque()
        self._pending_control: dict[int, tuple[MediaFrame, float]] = {}
        self._refresh_at = 0.0
        self._fresh_generation: int | None = None
        self._fresh_timestamp = 0
        self._fresh_arrival = 0.0
        self._media_validator: Callable[[MediaFrame], bool] | None = None

    def set_media_validator(self, validator: Callable[[MediaFrame], bool]) -> None:
        self._media_validator = validator

    async def open(self, grant: UdpGrant, *, media_id: bytes, media_epoch: int) -> None:
        if self._port is not None:
            raise TransportError("transport_already_open")
        self._replay = ReplayWindow()
        self._next_uplink = 0
        self._expected_downlink = 1
        self._reorder.clear()
        self._reorder_deadline_at = None
        self._non_audio_sequences.clear()
        self._ready.clear()
        self._pending_control.clear()
        self._fresh_generation = None
        result = self._factory(grant.host, grant.port)
        self._port = await result  # type: ignore[misc]
        self._grant = grant
        self._media_id = media_id
        self._media_epoch = media_epoch
        self._uplink = UdpCipher(grant.uplink_key, grant.uplink_salt)
        self._downlink = UdpCipher(grant.downlink_key, grant.downlink_salt)
        self._refresh_at = self._monotonic() + grant.refresh_after_ms / 1000
        try:
            await self._probe()
        except BaseException:
            await self.close()
            raise

    @property
    def refresh_due(self) -> bool:
        return self._grant is not None and self._refresh_remaining() <= 0

    async def send_audio(self, payload: bytes, *, timestamp: int) -> None:
        self._ensure_fresh()
        self._send(FLAG_AUDIO, payload, timestamp=timestamp, generation=0)

    async def send_keepalive(self, *, timestamp: int) -> None:
        self._ensure_fresh()
        self._send(FLAG_KEEPALIVE, b"", timestamp=timestamp, generation=0)

    async def receive_audio(self) -> MediaFrame:
        self._ensure_fresh()
        self._admit_pending_control()
        if self._ready:
            return self._ready.popleft()
        while True:
            refresh_remaining = self._refresh_remaining()
            if refresh_remaining <= 0:
                raise FreshReopenRequired()
            self._expire_receive_deadlines()
            if self._ready:
                return self._ready.popleft()
            now = self._monotonic()
            deadlines = [now + refresh_remaining]
            if self._reorder_deadline_at is not None:
                deadlines.append(self._reorder_deadline_at)
            if self._pending_control:
                earliest_pending = min(arrived_at for _, arrived_at in self._pending_control.values())
                deadlines.append(earliest_pending + self._max_media_age)
            timeout = max(0.0, min(deadlines) - now)
            try:
                datagram = await asyncio.wait_for(self._port_required().receive(), timeout=timeout)
            except TimeoutError:
                if self.refresh_due:
                    raise FreshReopenRequired() from None
                self._admit_pending_control()
                self._release_gap()
                if self._ready:
                    return self._ready.popleft()
                continue
            frame = self._accept_or_none(datagram)
            self._expire_receive_deadlines()
            if self._ready:
                return self._ready.popleft()
            if frame is None:
                continue
            if frame.flags != FLAG_AUDIO:
                self._buffer_non_audio(frame)
                if self._ready:
                    return self._ready.popleft()
                continue
            self._buffer(frame)
            if self._ready:
                return self._ready.popleft()

    async def close(self) -> None:
        port, self._port = self._port, None
        if port is not None:
            port.close()
        self._grant = None
        self._media_id = b""
        self._media_epoch = 0
        self._uplink = None
        self._downlink = None
        self._reorder.clear()
        self._reorder_deadline_at = None
        self._non_audio_sequences.clear()
        self._ready.clear()
        self._pending_control.clear()

    async def _probe(self) -> None:
        grant = self._grant
        if grant is None:
            raise TransportError("transport_not_open")
        deadline = self._monotonic() + grant.probe_timeout_ms / 1000
        probe = self._encode(FLAG_PROBE, b"", timestamp=0, generation=0, sequence=0)
        while self._monotonic() < deadline:
            self._port_required().send(probe)
            remaining = deadline - self._monotonic()
            try:
                datagram = await asyncio.wait_for(
                    self._port_required().receive(),
                    timeout=min(self._probe_retry, max(0.001, remaining)),
                )
            except TimeoutError:
                continue
            frame = self._accept_or_none(datagram, expected_probe_ack=True)
            if frame is None:
                continue
            if frame.flags == FLAG_PROBE_ACK:
                self._next_uplink = 1
                self._trace.emit("udp.probe.accepted", {"media_epoch": self._media_epoch})
                return
        raise TransportError("udp_probe_timeout", retryable=True)

    def _accept(self, datagram: bytes, *, expected_probe_ack: bool = False) -> MediaFrame | None:
        cipher = self._downlink
        if cipher is None:
            raise TransportError("transport_not_open")
        # Admission is committed only after identity and authentication succeed.
        header, _ = MediaFrame.decode_header(datagram, encrypted=True)
        if header.media_id != self._media_id or header.media_epoch != self._media_epoch:
            raise ProtocolError("stale_media_identity")
        if not self._replay.acceptable(header.sequence):
            raise ProtocolError("replayed_or_invalid_sequence")
        frame = cipher.decrypt(datagram)
        if expected_probe_ack and not (
            frame.flags == FLAG_PROBE_ACK
            and frame.sequence == 0
            and frame.timestamp == 0
            and frame.generation == 0
            and not frame.payload
        ):
            raise ProtocolError("invalid_probe_ack")
        if frame.flags == FLAG_AUDIO and self._media_validator is not None:
            try:
                admitted = self._media_validator(frame)
            except ProtocolError as exc:
                if exc.code != "unknown_media_generation":
                    raise
                if len(self._pending_control) >= 4 and frame.sequence not in self._pending_control:
                    raise FreshReopenRequired("UDP control/media ordering window exhausted") from exc
                self._pending_control.setdefault(frame.sequence, (frame, self._monotonic()))
                self._trace.emit("udp.audio.awaiting_control", {"sequence": frame.sequence})
                return None
            self._pending_control.pop(frame.sequence, None)
            self._replay.commit(frame.sequence)
            if not admitted:
                self._buffer_non_audio(frame)
                self._trace.emit("udp.audio.fenced", {"sequence": frame.sequence, "generation": frame.generation})
                return None
            return frame
        self._replay.commit(frame.sequence)
        return frame

    def _admit_pending_control(self) -> None:
        validator = self._media_validator
        if validator is None or not self._pending_control:
            return
        now = self._monotonic()
        for sequence in sorted(tuple(self._pending_control)):
            frame, arrived_at = self._pending_control[sequence]
            if now - arrived_at > self._max_media_age:
                raise FreshReopenRequired("UDP audio waited too long for response control")
            try:
                admitted = validator(frame)
            except ProtocolError as exc:
                if exc.code == "unknown_media_generation":
                    continue
                self._pending_control.pop(sequence, None)
                raise
            self._pending_control.pop(sequence, None)
            self._replay.commit(frame.sequence)
            if admitted:
                self._buffer(frame)
            else:
                self._buffer_non_audio(frame)

    def _accept_or_none(self, datagram: bytes, *, expected_probe_ack: bool = False) -> MediaFrame | None:
        try:
            return self._accept(datagram, expected_probe_ack=expected_probe_ack)
        except ProtocolError as exc:
            self._trace.emit("udp.datagram.rejected", {"code": exc.code})
            return None

    def _send(self, flags: int, payload: bytes, *, timestamp: int, generation: int) -> None:
        sequence = self._next_uplink
        if sequence > 0xFFFFFFFF:
            raise FreshReopenRequired("uplink sequence exhausted")
        self._port_required().send(self._encode(flags, payload, timestamp, generation, sequence))
        self._next_uplink += 1

    def _encode(self, flags: int, payload: bytes, timestamp: int, generation: int, sequence: int) -> bytes:
        cipher = self._uplink
        if cipher is None:
            raise TransportError("transport_not_open")
        return cipher.encrypt(
            MediaFrame(flags, self._media_id, self._media_epoch, sequence, timestamp, generation, payload)
        )

    def _buffer(self, frame: MediaFrame) -> None:
        self._check_freshness(frame)
        if frame.sequence < self._expected_downlink:
            return
        self._reorder[frame.sequence] = frame
        self._release_contiguous()
        self._arm_reorder_deadline()
        if len(self._reorder) + len(self._non_audio_sequences) >= 4:
            self._release_gap()

    def _buffer_non_audio(self, frame: MediaFrame) -> None:
        if frame.sequence < self._expected_downlink:
            return
        self._non_audio_sequences.add(frame.sequence)
        self._release_contiguous()
        self._arm_reorder_deadline()
        if len(self._reorder) + len(self._non_audio_sequences) >= 4:
            self._release_gap()

    def _release_contiguous(self) -> None:
        while True:
            if self._expected_downlink in self._non_audio_sequences:
                self._non_audio_sequences.remove(self._expected_downlink)
                self._expected_downlink += 1
                continue
            if self._expected_downlink in self._reorder:
                self._ready.append(self._reorder.pop(self._expected_downlink))
                self._expected_downlink += 1
                continue
            if not self._reorder and not self._non_audio_sequences:
                self._reorder_deadline_at = None
            return

    def _release_gap(self) -> None:
        pending = set(self._reorder) | self._non_audio_sequences
        if not pending:
            return
        sequence = min(pending)
        self._trace.emit(
            "udp.downlink.gap",
            {"missing_packets": max(0, sequence - self._expected_downlink), "next_sequence": sequence},
        )
        self._expected_downlink = sequence
        self._release_contiguous()

    def _arm_reorder_deadline(self) -> None:
        pending = set(self._reorder) | self._non_audio_sequences
        if pending and min(pending) > self._expected_downlink and self._reorder_deadline_at is None:
            self._reorder_deadline_at = self._monotonic() + self._max_media_age

    def _expire_receive_deadlines(self) -> None:
        now = self._monotonic()
        if self._pending_control:
            earliest = min(arrived_at for _, arrived_at in self._pending_control.values())
            if now >= earliest + self._max_media_age:
                raise FreshReopenRequired("UDP audio waited too long for response control")
        if self._reorder_deadline_at is not None and now >= self._reorder_deadline_at:
            self._release_gap()

    def _check_freshness(self, frame: MediaFrame) -> None:
        now = self._monotonic()
        if self._fresh_generation != frame.generation:
            self._fresh_generation = frame.generation
            self._fresh_timestamp = frame.timestamp
            self._fresh_arrival = now
            return
        media_elapsed = ((frame.timestamp - self._fresh_timestamp) & 0xFFFFFFFF) / 16_000
        age = now - self._fresh_arrival - media_elapsed
        if age > self._max_media_age:
            raise FreshReopenRequired("stale UDP media packet")

    def _ensure_fresh(self) -> None:
        if self._port is None:
            raise TransportError("transport_not_open")
        if self.refresh_due:
            raise FreshReopenRequired()

    def _refresh_remaining(self) -> float:
        grant = self._grant
        if grant is None:
            return 0.0
        return min(
            self._refresh_at - self._monotonic(),
            grant.expires_at_ms / 1_000 - self._wall_clock(),
        )

    def _port_required(self) -> DatagramPort:
        if self._port is None:
            raise TransportError("transport_not_open")
        return self._port


class _QueueProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=64)
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, _addr: tuple[str, int]) -> None:
        if not self.queue.full():
            self.queue.put_nowait(data)


class _AsyncioPort:
    def __init__(self, protocol: _QueueProtocol, transport: asyncio.DatagramTransport) -> None:
        self._protocol = protocol
        self._transport = transport

    def send(self, data: bytes) -> None:
        self._transport.sendto(data)

    async def receive(self) -> bytes:
        return await self._protocol.queue.get()

    def close(self) -> None:
        self._transport.close()


async def _create_port(host: str, port: int) -> DatagramPort:
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(_QueueProtocol, remote_addr=(host, port))
    return _AsyncioPort(protocol, transport)  # type: ignore[arg-type]
