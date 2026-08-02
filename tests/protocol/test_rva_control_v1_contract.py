from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = ROOT / "protocol" / "rva_v1"


def load_json(name: str) -> dict[str, Any]:
    return json.loads((PROTOCOL / name).read_text(encoding="utf-8"))


def make_validator(schema: dict[str, Any]) -> Draft202012Validator:
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def test_contract_registry_defines_complete_v1_control_surface() -> None:
    contract = yaml.safe_load((PROTOCOL / "contract.yaml").read_text(encoding="utf-8"))
    assert {entry["type"] for entry in contract["messages"]} == {
        "session.open",
        "session.opened",
        "transcript.delta",
        "transcript.final",
        "response.begin",
        "response.text",
        "response.end",
        "playback.stop",
        "playback.started",
        "playback.ended",
        "response.cancel.request",
        "session.error",
        "session.close",
    }
    assert contract["protocol"] == {
        "id": "rva/1",
        "version": 1,
        "transport": "websocket",
        "path": "/rva/v1/voice",
        "schema": "messages.schema.json",
        "max_control_message_bytes": 32768,
        "heartbeat_interval_ms": {"default": 15000, "minimum": 5000, "maximum": 60000},
        "idle_timeout_ms": {"default": 45000, "minimum": 15000, "maximum": 180000},
    }


def test_terminal_and_cancel_fields_match_the_frozen_contract() -> None:
    schema = load_json("messages.schema.json")
    defs = schema["$defs"]
    assert defs["responseEnd"]["properties"]["outcome"]["enum"] == [
        "completed",
        "cancelled",
        "failed",
    ]
    assert defs["playbackStop"]["properties"]["cause"]["enum"] == [
        "explicit_user_request",
        "recognized_interrupt",
        "session_close",
        "response_failed",
    ]
    assert defs["responseCancelRequest"]["properties"]["cause"]["const"] == "user_request"
    assert defs["playbackEnded"]["properties"]["outcome"]["enum"] == [
        "completed",
        "stopped",
        "failed",
    ]


def test_media_profiles_share_v1_header_and_zero_uplink_generation() -> None:
    contract = yaml.safe_load((PROTOCOL / "contract.yaml").read_text(encoding="utf-8"))
    profiles = {profile["id"]: profile for profile in contract["media_profiles"]}
    assert set(profiles) == {"wss-opus/1", "udp-opus-gcm/1"}
    assert all(profile["control"] == "rva/1" for profile in profiles.values())
    assert all(profile["uplink_generation"] == 0 for profile in profiles.values())
    assert contract["media_header"]["bytes"] == 32
    assert contract["media_header"]["wire_version"] == 1
    fields = {field["name"]: field for field in contract["media_header"]["fields"]}
    assert fields["generation"]["offset"] == 24
    udp = profiles["udp-opus-gcm/1"]
    assert udp["cipher"] == "AES-128-GCM"
    assert udp["directional_keys"] is True
    assert udp["tag_bytes"] == 16
    assert udp["retransmission"] is False


def test_udp_v1_canonical_byte_wire_has_zero_uplink_generation() -> None:
    contract = yaml.safe_load((PROTOCOL / "contract.yaml").read_text(encoding="utf-8"))
    canonical = json.loads(
        (ROOT / "protocol" / "udp_opus_gcm_v1" / "fixtures" / "positive.json").read_text(
            encoding="utf-8"
        )
    )
    udp = next(profile for profile in contract["media_profiles"] if profile["id"] == "udp-opus-gcm/1")
    magic = next(field for field in contract["media_header"]["fields"] if field["name"] == "magic")
    for vector in canonical["vectors"]:
        header = bytes.fromhex(vector["header_hex"])
        assert header[:2].hex() == magic["value_hex"]
        assert header[2] == 1
        if vector["direction"] == "uplink":
            assert vector["fields"]["generation"] == 0
    assert canonical["header_bytes"] == contract["media_header"]["bytes"]
    assert canonical["tag_bytes"] == udp["tag_bytes"]
    assert canonical["max_payload_bytes"] == udp["max_payload_bytes"]
    assert canonical["max_datagram_bytes"] == udp["max_datagram_bytes"]


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
    vectors = load_json("fixtures/positive.json")["vectors"]
    assert len({vector["id"] for vector in vectors}) == len(vectors)
    assert all(validator.is_valid(vector["message"]) for vector in vectors)
    assert {vector["message"]["type"] for vector in vectors} == {
        definition["properties"]["type"]["const"]
        for definition in schema["$defs"].values()
        if isinstance(definition, dict) and "type" in definition.get("properties", {})
    }


def test_positive_fixture_directions_match_contract() -> None:
    contract = yaml.safe_load((PROTOCOL / "contract.yaml").read_text(encoding="utf-8"))
    directions = {entry["type"]: entry["direction"] for entry in contract["messages"]}
    for vector in load_json("fixtures/positive.json")["vectors"]:
        declared = directions[vector["message"]["type"]]
        assert declared == "bidirectional" or vector["direction"] == declared


def test_all_negative_schema_fixtures_are_rejected() -> None:
    validator = make_validator(load_json("messages.schema.json"))
    vectors = load_json("fixtures/negative.json")["schema_vectors"]
    assert len({vector["id"] for vector in vectors}) == len(vectors)
    assert all(not validator.is_valid(vector["message"]) for vector in vectors)


def test_raw_json_negative_fixture_really_contains_duplicate_fields() -> None:
    vectors = load_json("fixtures/negative.json")["raw_json_vectors"]

    def reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate field")
            result[key] = value
        return result

    for vector in vectors:
        with pytest.raises(ValueError, match="duplicate field"):
            json.loads(vector["json_text"], object_pairs_hook=reject_duplicate)


def test_semantic_negative_fixtures_cover_every_state_rule() -> None:
    contract = yaml.safe_load((PROTOCOL / "contract.yaml").read_text(encoding="utf-8"))
    fixture = load_json("fixtures/negative.json")
    rules = {rule["id"]: rule["violation"] for rule in contract["state_rules"]}
    covered = {vector["rule"]: vector["reason"] for vector in fixture["semantic_vectors"]}
    assert covered == rules


def test_opened_profile_requires_exactly_the_matching_transport_material() -> None:
    validator = make_validator(load_json("messages.schema.json"))
    positive = load_json("fixtures/positive.json")["vectors"]
    opened = {
        vector["id"]: vector["message"]
        for vector in positive
        if vector["message"]["type"] == "session.opened"
    }
    assert "udp_grant" not in opened["opened-wss-v1"]
    assert "udp_grant" in opened["opened-udp-v1"]
    assert validator.is_valid(opened["opened-wss-v1"])
    assert validator.is_valid(opened["opened-udp-v1"])
