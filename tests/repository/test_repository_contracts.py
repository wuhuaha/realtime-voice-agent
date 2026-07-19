from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "verify_repository", ROOT / "scripts" / "verify_repository.py"
)
assert SPEC is not None and SPEC.loader is not None
VERIFY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY)


def test_forbidden_paths_are_rejected() -> None:
    errors = VERIFY.validate_tracked_paths(
        (
            "server/direct_webrtc_v1/peer.py",
            "firmware/.env.local",
            "artifacts/device.bin",
        )
    )
    assert len(errors) == 3


def test_allowed_templates_are_not_reported() -> None:
    assert VERIFY.validate_tracked_paths((".env.example", "firmware/device/sdkconfig.defaults")) == []


def test_protocol_contract_is_consistent() -> None:
    assert VERIFY.validate_protocol(ROOT) == []
