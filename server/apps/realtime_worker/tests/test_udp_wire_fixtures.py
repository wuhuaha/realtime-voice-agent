from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from realtime_worker.transport.udp_gateway import (
    UDP_HEADER_BYTES,
    UDP_MAX_DATAGRAM_BYTES,
    UDP_MAX_PAYLOAD_BYTES,
    UDP_TAG_BYTES,
    UdpPacketHeader,
)

pytestmark = pytest.mark.contract

FIXTURES = Path(__file__).resolve().parents[4] / "protocol" / "udp_opus_gcm_v1" / "fixtures"


def _load(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_positive_wire_vectors_match_runtime_codec_byte_for_byte() -> None:
    fixture = _load("positive.json")
    assert fixture["profile"] == "udp-opus-gcm-v1"
    assert fixture["wire_version"] == 1
    assert fixture["header_bytes"] == UDP_HEADER_BYTES == 32
    assert fixture["tag_bytes"] == UDP_TAG_BYTES == 16
    assert fixture["max_datagram_bytes"] == UDP_MAX_DATAGRAM_BYTES == 1280
    assert fixture["max_payload_bytes"] == UDP_MAX_PAYLOAD_BYTES == 1200

    vectors = fixture["vectors"]
    assert isinstance(vectors, list)
    assert len({vector["id"] for vector in vectors}) == len(vectors)
    for vector in vectors:
        fields = vector["fields"]
        payload = bytes.fromhex(vector["payload_hex"])
        header = UdpPacketHeader(
            flags=fields["flags"],
            media_id=bytes.fromhex(fields["media_id_hex"]),
            media_epoch=fields["media_epoch"],
            sequence=fields["sequence"],
            timestamp=fields["timestamp"],
            generation=fields["generation"],
            payload_length=fields["payload_length"],
        )
        expected_header = bytes.fromhex(vector["header_hex"])
        expected_encrypted = bytes.fromhex(vector["ciphertext_and_tag_hex"])
        expected_datagram = bytes.fromhex(vector["datagram_hex"])
        nonce = bytes.fromhex(vector["nonce_hex"])

        assert header.encode() == expected_header, vector["id"]
        assert expected_header + expected_encrypted == expected_datagram, vector["id"]
        assert bytes.fromhex(vector["salt_hex"]) + fields["sequence"].to_bytes(4, "big") == nonce
        assert (
            AESGCM(bytes.fromhex(vector["key_hex"])).encrypt(nonce, payload, expected_header) == expected_encrypted
        ), vector["id"]

        decoded, encrypted = UdpPacketHeader.decode(expected_datagram)
        assert decoded == header, vector["id"]
        assert encrypted == expected_encrypted, vector["id"]
        assert AESGCM(bytes.fromhex(vector["key_hex"])).decrypt(nonce, encrypted, expected_header) == payload, vector[
            "id"
        ]


def test_negative_wire_vectors_fail_at_declared_layer() -> None:
    fixture = _load("negative.json")
    key = bytes.fromhex(fixture["key_hex"])
    salt = bytes.fromhex(fixture["salt_hex"])
    vectors = fixture["vectors"]
    assert isinstance(vectors, list)
    assert len({vector["id"] for vector in vectors}) == len(vectors)

    for vector in vectors:
        datagram = bytes.fromhex(vector["datagram_hex"])
        if vector["reject_stage"] == "parser":
            with pytest.raises(ValueError):
                UdpPacketHeader.decode(datagram)
            continue

        assert vector["reject_stage"] == "authentication"
        header, encrypted = UdpPacketHeader.decode(datagram)
        nonce = salt + header.sequence.to_bytes(4, "big")
        with pytest.raises(InvalidTag):
            AESGCM(key).decrypt(nonce, encrypted, datagram[:UDP_HEADER_BYTES])
