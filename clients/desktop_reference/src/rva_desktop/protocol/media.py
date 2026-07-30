from __future__ import annotations

import struct
from dataclasses import dataclass

from ..errors import ProtocolError

MAGIC = b"VA"
WIRE_VERSION = 2
HEADER_BYTES = 32
MAX_PAYLOAD_BYTES = 1200
MAX_WSS_FRAME_BYTES = HEADER_BYTES + MAX_PAYLOAD_BYTES
MAX_UDP_DATAGRAM_BYTES = 1280
GCM_TAG_BYTES = 16

FLAG_AUDIO = 1
FLAG_PROBE = 2
FLAG_PROBE_ACK = 4
FLAG_KEEPALIVE = 8
_VALID_FLAGS = {FLAG_AUDIO, FLAG_PROBE, FLAG_PROBE_ACK, FLAG_KEEPALIVE}
_HEADER = struct.Struct("!2sBB8sIIIII")


@dataclass(frozen=True, slots=True)
class PlaybackTarget:
    response_id: str
    generation: int


@dataclass(frozen=True, slots=True)
class MediaFrame:
    flags: int
    media_id: bytes
    media_epoch: int
    sequence: int
    timestamp: int
    generation: int
    payload: bytes = b""

    def header(self) -> bytes:
        if self.flags not in _VALID_FLAGS:
            raise ProtocolError("invalid_media_flags")
        if len(self.media_id) != 8:
            raise ProtocolError("invalid_media_id")
        _uint32("media_epoch", self.media_epoch, minimum=1)
        _uint32("sequence", self.sequence)
        _uint32("timestamp", self.timestamp)
        _uint32("generation", self.generation)
        if len(self.payload) > MAX_PAYLOAD_BYTES:
            raise ProtocolError("invalid_media_payload")
        if self.flags == FLAG_AUDIO and not self.payload:
            raise ProtocolError("invalid_media_payload")
        if self.flags != FLAG_AUDIO and self.payload:
            raise ProtocolError("non_audio_payload")
        if self.flags != FLAG_AUDIO and self.generation != 0:
            raise ProtocolError("invalid_media_generation")
        return _HEADER.pack(
            MAGIC,
            WIRE_VERSION,
            self.flags,
            self.media_id,
            self.media_epoch,
            self.sequence,
            self.timestamp,
            self.generation,
            len(self.payload),
        )

    def encode_plain(self) -> bytes:
        return self.header() + self.payload

    @classmethod
    def decode_plain(cls, wire: bytes) -> MediaFrame:
        frame, tail = cls.decode_header(wire, encrypted=False)
        return cls(**_frame_fields(frame), payload=tail)

    @classmethod
    def decode_header(cls, wire: bytes, *, encrypted: bool) -> tuple[MediaFrame, bytes]:
        minimum = HEADER_BYTES + (GCM_TAG_BYTES if encrypted else 0)
        maximum = MAX_UDP_DATAGRAM_BYTES if encrypted else MAX_WSS_FRAME_BYTES
        if len(wire) < minimum:
            raise ProtocolError("truncated_media_header")
        if len(wire) > maximum:
            raise ProtocolError("media_frame_too_large")
        magic, version, flags, media_id, media_epoch, sequence, timestamp, generation, length = (
            _HEADER.unpack_from(wire)
        )
        if magic != MAGIC or version != WIRE_VERSION:
            raise ProtocolError("unsupported_media_header")
        tail = wire[HEADER_BYTES:]
        expected = length + (GCM_TAG_BYTES if encrypted else 0)
        if len(tail) != expected:
            raise ProtocolError("media_length_mismatch")
        if flags not in _VALID_FLAGS:
            raise ProtocolError("invalid_media_flags")
        if len(media_id) != 8:
            raise ProtocolError("invalid_media_id")
        _uint32("media_epoch", media_epoch, minimum=1)
        _uint32("sequence", sequence)
        _uint32("timestamp", timestamp)
        _uint32("generation", generation)
        if flags != FLAG_AUDIO and generation != 0:
            raise ProtocolError("invalid_media_generation")
        frame = cls(flags, media_id, media_epoch, sequence, timestamp, generation, b"")
        if flags == FLAG_AUDIO and length == 0:
            raise ProtocolError("invalid_media_payload")
        if length > MAX_PAYLOAD_BYTES:
            raise ProtocolError("invalid_media_payload")
        return frame, tail


def _frame_fields(frame: MediaFrame) -> dict[str, object]:
    return {
        "flags": frame.flags,
        "media_id": frame.media_id,
        "media_epoch": frame.media_epoch,
        "sequence": frame.sequence,
        "timestamp": frame.timestamp,
        "generation": frame.generation,
    }


def _uint32(field: str, value: int, *, minimum: int = 0) -> None:
    if type(value) is not int or not minimum <= value <= 0xFFFFFFFF:
        raise ProtocolError(f"invalid_{field}")
