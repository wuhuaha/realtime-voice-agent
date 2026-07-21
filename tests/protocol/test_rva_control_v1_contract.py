from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = ROOT / "protocol" / "rva_control_v1"


def load_json(name: str) -> dict[str, Any]:
    return json.loads((PROTOCOL / name).read_text(encoding="utf-8"))


def make_validator(schema: dict[str, Any]) -> Draft202012Validator:
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def test_contract_registry_defines_complete_control_surface() -> None:
    contract = yaml.safe_load((PROTOCOL / "contract.yaml").read_text(encoding="utf-8"))
    messages = contract["messages"]
    message_types = {entry["type"] for entry in messages}
    assert message_types == {
        "session.open",
        "session.opened",
        "transcript.delta",
        "transcript.final",
        "response.begin",
        "response.text",
        "response.end",
        "response.cancel",
        "response.cancelled",
        "session.error",
        "session.close",
    }
    assert contract["protocol"] == {
        "id": "rva-control-v1",
        "version": 1,
        "transport": "websocket",
        "path": "/v1/voice",
        "schema": "messages.schema.json",
        "max_control_message_bytes": 32768,
        "heartbeat_interval_ms": {"default": 15000, "minimum": 5000, "maximum": 60000},
        "idle_timeout_ms": {"default": 45000, "minimum": 15000, "maximum": 180000},
    }
    assert contract["identifiers"]["session_epoch"]["type"] == "string"
    assert contract["identifiers"]["media_epoch"] == {
        "type": "uint32",
        "minimum": 1,
        "semantics": "Changes whenever media admission is reopened; stale media epochs are rejected.",
    }


def test_media_profiles_share_generation_aware_header() -> None:
    contract = yaml.safe_load((PROTOCOL / "contract.yaml").read_text(encoding="utf-8"))
    profiles = {profile["id"]: profile for profile in contract["media_profiles"]}
    assert set(profiles) == {"wss-opus-v2", "udp-opus-gcm-v1"}
    assert all(profile["control"] == "rva-control-v1" for profile in profiles.values())
    assert contract["media_header"]["bytes"] == 32
    fields = {field["name"]: field for field in contract["media_header"]["fields"]}
    assert fields["media_epoch"]["offset"] == 12
    assert fields["sequence"]["offset"] == 16
    assert fields["timestamp"]["offset"] == 20
    assert fields["generation"]["offset"] == 24
    assert profiles["udp-opus-gcm-v1"]["cipher"] == "AES-128-GCM"
    assert profiles["udp-opus-gcm-v1"]["directional_keys"] is True
    assert profiles["udp-opus-gcm-v1"]["tag_bytes"] == 16
    assert profiles["udp-opus-gcm-v1"]["retransmission"] is False


def test_shared_udp_profile_preserves_the_canonical_byte_wire() -> None:
    contract = yaml.safe_load((PROTOCOL / "contract.yaml").read_text(encoding="utf-8"))
    canonical = json.loads(
        (ROOT / "protocol" / "xiaozhi_udp_v1" / "fixtures" / "positive.json").read_text(encoding="utf-8")
    )
    udp = next(profile for profile in contract["media_profiles"] if profile["id"] == "udp-opus-gcm-v1")
    first_header = bytes.fromhex(canonical["vectors"][0]["header_hex"])
    magic = next(field for field in contract["media_header"]["fields"] if field["name"] == "magic")

    assert first_header[:2].hex() == magic["value_hex"]
    assert canonical["header_bytes"] == contract["media_header"]["bytes"]
    assert canonical["tag_bytes"] == udp["tag_bytes"]
    assert canonical["max_payload_bytes"] == udp["max_payload_bytes"]
    assert canonical["max_datagram_bytes"] == udp["max_datagram_bytes"]
    uplink_audio = next(vector for vector in canonical["vectors"] if vector["id"] == "uplink-audio-sequence-1")
    assert uplink_audio["fields"]["generation"] == 1
    assert udp["uplink_probe_generation"] == 0
    assert udp["uplink_audio_generation"] == "active_playback_generation"


def test_schema_references_and_event_discriminators_are_complete() -> None:
    schema = load_json("messages.schema.json")
    make_validator(schema)
    referenced = {item["$ref"].removeprefix("#/$defs/") for item in schema["oneOf"]}
    discriminators = {schema["$defs"][name]["properties"]["type"]["const"] for name in referenced}
    assert len(referenced) == len(schema["oneOf"])
    assert len(discriminators) == len(referenced)
    for definition in schema["$defs"].values():
        for match in re.findall(r'"\$ref":\s*"#\/\$defs\/([^\"]+)"', json.dumps(definition)):
            assert match in schema["$defs"]


def test_all_positive_fixtures_match_exactly_one_message_schema() -> None:
    schema = load_json("messages.schema.json")
    validator = make_validator(schema)
    fixture = load_json("fixtures/positive.json")
    ids = [vector["id"] for vector in fixture["vectors"]]
    assert len(ids) == len(set(ids))
    assert all(validator.is_valid(vector["message"]) for vector in fixture["vectors"])
    assert {vector["message"]["type"] for vector in fixture["vectors"]} == {
        definition["properties"]["type"]["const"]
        for definition in schema["$defs"].values()
        if isinstance(definition, dict) and "type" in definition.get("properties", {})
    }


def test_positive_fixture_directions_match_registry() -> None:
    contract = yaml.safe_load((PROTOCOL / "contract.yaml").read_text(encoding="utf-8"))
    directions = {entry["type"]: entry["direction"] for entry in contract["messages"]}
    for vector in load_json("fixtures/positive.json")["vectors"]:
        declared = directions[vector["message"]["type"]]
        assert declared == "bidirectional" or vector["direction"] == declared


def test_all_negative_schema_fixtures_are_rejected() -> None:
    schema = load_json("messages.schema.json")
    validator = make_validator(schema)
    fixture = load_json("fixtures/negative.json")
    vectors = fixture["schema_vectors"]
    assert len({vector["id"] for vector in vectors}) == len(vectors)
    assert all(not validator.is_valid(vector["message"]) for vector in vectors)


def test_semantic_negative_fixtures_cover_every_state_rule() -> None:
    contract = yaml.safe_load((PROTOCOL / "contract.yaml").read_text(encoding="utf-8"))
    fixture = load_json("fixtures/negative.json")
    rules = {rule["id"]: rule["violation"] for rule in contract["state_rules"]}
    covered = {vector["rule"]: vector["reason"] for vector in fixture["semantic_vectors"]}
    assert covered == rules


def test_opened_profile_requires_exactly_the_matching_transport_material() -> None:
    schema = load_json("messages.schema.json")
    validator = make_validator(schema)
    positive = load_json("fixtures/positive.json")["vectors"]
    opened = {vector["id"]: vector["message"] for vector in positive if vector["message"]["type"] == "session.opened"}
    assert "udp_grant" not in opened["opened-wss"]
    assert "udp_grant" in opened["opened-udp"]
    assert opened["opened-wss"]["media_id"] == "0123456789abcdef"
    assert opened["opened-wss"]["media_epoch"] == 7
    assert opened["opened-udp"]["media_id"] == "fedcba9876543210"
    assert opened["opened-udp"]["media_epoch"] == 8
    assert "media_id" not in opened["opened-udp"]["udp_grant"]
    assert validator.is_valid(opened["opened-wss"])
    assert validator.is_valid(opened["opened-udp"])
