from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from realtime_worker.transport.udp_wire import UdpPacketHeader

FIXTURES = Path(__file__).resolve().parents[4] / "protocol" / "udp_opus_gcm_v1" / "fixtures"


def _fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.mark.contract
def test_udp_v1_positive_vectors_match_canonical_bytes() -> None:
    fixture = _fixture("positive.json")
    for vector in fixture["vectors"]:
        fields = vector["fields"]
        header = UdpPacketHeader(
            flags=fields["flags"],
            media_id=bytes.fromhex(fields["media_id_hex"]),
            media_epoch=fields["media_epoch"],
            sequence=fields["sequence"],
            timestamp=fields["timestamp"],
            generation=fields["generation"],
            payload_length=fields["payload_length"],
        )
        assert header.encode().hex() == vector["header_hex"]
        decoded, encrypted = UdpPacketHeader.decode(bytes.fromhex(vector["datagram_hex"]))
        nonce = bytes.fromhex(vector["salt_hex"]) + decoded.sequence.to_bytes(4, "big")
        payload = AESGCM(bytes.fromhex(vector["key_hex"])).decrypt(nonce, encrypted, header.encode())
        assert decoded == header
        assert nonce.hex() == vector["nonce_hex"]
        assert encrypted.hex() == vector["ciphertext_and_tag_hex"]
        assert payload.hex() == vector["payload_hex"]


@pytest.mark.contract
def test_udp_v1_negative_vectors_fail_at_declared_stage() -> None:
    positive = _fixture("positive.json")
    keys = {
        direction: next(vector for vector in positive["vectors"] if vector["direction"] == direction)
        for direction in ("uplink", "downlink")
    }
    fixture = _fixture("negative.json")
    for vector in fixture["vectors"]:
        datagram = bytes.fromhex(vector["datagram_hex"])
        if vector["reject_stage"] == "parser":
            with pytest.raises(ValueError):
                UdpPacketHeader.decode(datagram)
            continue
        header, encrypted = UdpPacketHeader.decode(datagram)
        direction = "downlink" if "probe-ack" in vector["id"] else "uplink"
        key_material = keys[direction]
        nonce = bytes.fromhex(key_material["salt_hex"]) + header.sequence.to_bytes(4, "big")
        if vector["reject_stage"] == "authentication":
            with pytest.raises(InvalidTag):
                AESGCM(bytes.fromhex(key_material["key_hex"])).decrypt(
                    nonce,
                    encrypted,
                    datagram[:32],
                )
        else:
            AESGCM(bytes.fromhex(key_material["key_hex"])).decrypt(nonce, encrypted, datagram[:32])
            assert vector["reject_stage"] == "generation"
            assert header.generation != 0
