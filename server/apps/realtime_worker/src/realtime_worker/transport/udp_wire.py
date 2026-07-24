"""Canonical packet primitives for the udp-opus-gcm-v2 media profile."""

from __future__ import annotations

import struct
from dataclasses import dataclass

UDP_MAGIC = b"VA"
UDP_VERSION = 2
UDP_FLAG_AUDIO = 0x01
UDP_FLAG_PROBE = 0x02
UDP_FLAG_PROBE_ACK = 0x04
UDP_FLAG_KEEPALIVE = 0x08
UDP_TAG_BYTES = 16
UDP_SALT_BYTES = 8
UDP_KEY_BYTES = 16
UDP_HEADER = struct.Struct("!2sBB8sIIIII")
UDP_HEADER_BYTES = UDP_HEADER.size
UDP_MAX_DATAGRAM_BYTES = 1280
UDP_MAX_PAYLOAD_BYTES = 1200
UDP_REPLAY_WINDOW_PACKETS = 64
UDP_MAX_SEQUENCE_FORWARD_JUMP = 1024
UDP_JITTER_WINDOW_PACKETS = 4


@dataclass(frozen=True, slots=True)
class UdpPacketHeader:
    flags: int
    media_id: bytes
    media_epoch: int
    sequence: int
    timestamp: int
    generation: int
    payload_length: int

    def encode(self) -> bytes:
        return UDP_HEADER.pack(
            UDP_MAGIC,
            UDP_VERSION,
            self.flags,
            self.media_id,
            self.media_epoch,
            self.sequence,
            self.timestamp,
            self.generation,
            self.payload_length,
        )

    @classmethod
    def decode(cls, datagram: bytes) -> tuple[UdpPacketHeader, bytes]:
        if len(datagram) < UDP_HEADER_BYTES + UDP_TAG_BYTES:
            raise ValueError("UDP datagram is shorter than header and tag")
        magic, version, flags, media_id, epoch, sequence, timestamp, generation, payload_length = (
            UDP_HEADER.unpack_from(datagram)
        )
        if magic != UDP_MAGIC or version != UDP_VERSION:
            raise ValueError("unsupported UDP media packet")
        if epoch == 0:
            raise ValueError("UDP media epoch must be non-zero")
        if payload_length > UDP_MAX_PAYLOAD_BYTES:
            raise ValueError("UDP payload is too large")
        encrypted = datagram[UDP_HEADER_BYTES:]
        if len(encrypted) != payload_length + UDP_TAG_BYTES:
            raise ValueError("UDP payload length does not match datagram")
        return (
            cls(flags, media_id, epoch, sequence, timestamp, generation, payload_length),
            encrypted,
        )


class ReplayWindow:
    """64-packet replay history with a bounded forward admission jump."""

    def __init__(self) -> None:
        self._highest = -1
        self._bitmap = 0

    def can_accept(self, sequence: int) -> bool:
        if self._highest < 0:
            return True
        if sequence > self._highest:
            return sequence - self._highest <= UDP_MAX_SEQUENCE_FORWARD_JUMP
        distance = self._highest - sequence
        return distance < UDP_REPLAY_WINDOW_PACKETS and (self._bitmap & (1 << distance)) == 0

    def exceeds_forward_window(self, sequence: int) -> bool:
        return self._highest >= 0 and sequence > self._highest + UDP_MAX_SEQUENCE_FORWARD_JUMP

    def commit(self, sequence: int) -> None:
        if not self.can_accept(sequence):
            raise ValueError("sequence is outside replay window or duplicated")
        if sequence > self._highest:
            shift = sequence - self._highest
            self._bitmap = (
                1
                if shift >= UDP_REPLAY_WINDOW_PACKETS
                else ((self._bitmap << shift) | 1) & ((1 << UDP_REPLAY_WINDOW_PACKETS) - 1)
            )
            self._highest = sequence
            return
        self._bitmap |= 1 << (self._highest - sequence)


__all__ = [
    "UDP_FLAG_AUDIO",
    "UDP_FLAG_KEEPALIVE",
    "UDP_FLAG_PROBE",
    "UDP_FLAG_PROBE_ACK",
    "UDP_HEADER_BYTES",
    "UDP_KEY_BYTES",
    "UDP_JITTER_WINDOW_PACKETS",
    "UDP_MAX_DATAGRAM_BYTES",
    "UDP_MAX_PAYLOAD_BYTES",
    "UDP_MAX_SEQUENCE_FORWARD_JUMP",
    "UDP_REPLAY_WINDOW_PACKETS",
    "UDP_SALT_BYTES",
    "UDP_TAG_BYTES",
    "ReplayWindow",
    "UdpPacketHeader",
]
