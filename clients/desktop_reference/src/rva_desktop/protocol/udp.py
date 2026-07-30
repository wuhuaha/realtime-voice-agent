from __future__ import annotations

from dataclasses import replace

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..errors import ProtocolError
from .media import HEADER_BYTES, MediaFrame

REPLAY_WINDOW_PACKETS = 64
MAX_SEQUENCE_FORWARD_JUMP = 1024


class ReplayWindow:
    """Authenticate first, then commit sequence admission."""

    def __init__(self) -> None:
        self._highest: int | None = None
        self._bitmap = 0

    @property
    def highest(self) -> int | None:
        return self._highest

    def acceptable(self, sequence: int) -> bool:
        if not 0 <= sequence <= 0xFFFFFFFF:
            return False
        if self._highest is None:
            return True
        if sequence > self._highest:
            return sequence - self._highest <= MAX_SEQUENCE_FORWARD_JUMP
        offset = self._highest - sequence
        return offset < REPLAY_WINDOW_PACKETS and (self._bitmap & (1 << offset)) == 0

    def commit(self, sequence: int) -> None:
        if not self.acceptable(sequence):
            raise ProtocolError("replayed_or_invalid_sequence")
        if self._highest is None:
            self._highest = sequence
            self._bitmap = 1
            return
        if sequence > self._highest:
            shift = sequence - self._highest
            self._bitmap = ((self._bitmap << shift) | 1) & ((1 << REPLAY_WINDOW_PACKETS) - 1)
            self._highest = sequence
            return
        self._bitmap |= 1 << (self._highest - sequence)


class UdpCipher:
    def __init__(self, key: bytes, salt: bytes) -> None:
        if len(key) != 16:
            raise ValueError("AES-GCM key must contain 16 bytes")
        if len(salt) != 8:
            raise ValueError("AES-GCM salt must contain 8 bytes")
        self._cipher = AESGCM(key)
        self._salt = salt

    def encrypt(self, frame: MediaFrame) -> bytes:
        aad = frame.header()
        nonce = self._salt + frame.sequence.to_bytes(4, "big")
        return aad + self._cipher.encrypt(nonce, frame.payload, aad)

    def decrypt(self, datagram: bytes) -> MediaFrame:
        frame, encrypted = MediaFrame.decode_header(datagram, encrypted=True)
        nonce = self._salt + frame.sequence.to_bytes(4, "big")
        try:
            payload = self._cipher.decrypt(nonce, encrypted, datagram[:HEADER_BYTES])
        except InvalidTag as exc:
            raise ProtocolError("udp_authentication_failed") from exc
        return replace(frame, payload=payload)
