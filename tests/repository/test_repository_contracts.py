from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

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


def test_manifest_rejects_unhashed_traceability_path(tmp_path: Path) -> None:
    manifest_root = tmp_path / "repository"
    baseline = manifest_root / "migration" / "baseline"
    baseline.mkdir(parents=True)
    fixture = manifest_root / "fixture.txt"
    fixture.write_text("fixture\n", encoding="utf-8")
    manifest = {
        "files": [{"production_path": "fixture.txt", "sha256": VERIFY.sha256(fixture)}],
        "traceability": {"udp_wire_contract": ["missing.txt"]},
    }
    (baseline / "source-manifest.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")

    assert VERIFY.validate_manifest(manifest_root) == [
        "traceability path is not hashed in manifest: udp_wire_contract: missing.txt"
    ]


def test_manifest_rejects_duplicate_paths_and_invalid_digest(tmp_path: Path) -> None:
    manifest_root = tmp_path / "repository"
    baseline = manifest_root / "migration" / "baseline"
    baseline.mkdir(parents=True)
    fixture = manifest_root / "fixture.txt"
    fixture.write_text("fixture\n", encoding="utf-8")
    manifest = {
        "files": [
            {"production_path": "fixture.txt", "sha256": "not-a-digest"},
            {"production_path": "fixture.txt", "sha256": VERIFY.sha256(fixture)},
        ]
    }
    (baseline / "source-manifest.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")

    assert VERIFY.validate_manifest(manifest_root) == [
        "invalid manifest SHA256: fixture.txt",
        "duplicate manifest production_path: fixture.txt",
    ]


def test_manifest_requires_historical_source_path_for_production_firmware(tmp_path: Path) -> None:
    manifest_root = tmp_path / "repository"
    baseline = manifest_root / "migration" / "baseline"
    target = manifest_root / "firmware" / "targets" / "lichuang-dev"
    baseline.mkdir(parents=True)
    target.mkdir(parents=True)
    fixture = target / "fixture.txt"
    fixture.write_text("fixture\n", encoding="utf-8")
    manifest = {
        "files": [
            {
                "production_path": "firmware/targets/lichuang-dev/fixture.txt",
                "sha256": VERIFY.sha256(fixture),
            }
        ]
    }
    (baseline / "source-manifest.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")

    assert VERIFY.validate_manifest(manifest_root) == [
        "firmware provenance path mismatch: firmware/targets/lichuang-dev/fixture.txt"
    ]


def test_repository_firmware_composition_is_explicit() -> None:
    assert VERIFY.validate_firmware_composition(ROOT) == []


def test_firmware_source_lock_identity_rejects_cross_file_drift(tmp_path: Path) -> None:
    controlled = tmp_path / "firmware" / "locks" / "xiaozhi-esp32.dependencies.lock"
    source_lock = tmp_path / "third_party" / "sources.lock.yaml"
    manifest = tmp_path / "migration" / "baseline" / "source-manifest.yaml"
    controlled.parent.mkdir(parents=True)
    source_lock.parent.mkdir(parents=True)
    manifest.parent.mkdir(parents=True)
    controlled.write_text("locked\n", encoding="utf-8")
    source_lock.write_text(
        yaml.safe_dump(
            {
                "sources": [
                    {
                        "id": "xiaozhi-esp32",
                        "dependency_lock_sha256": "0" * 64,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    manifest.write_text(
        yaml.safe_dump({"upstream": {"xiaozhi_dependencies_lock_sha256": "1" * 64}}),
        encoding="utf-8",
    )

    assert VERIFY.validate_firmware_source_lock(tmp_path) == [
        "third_party source lock differs from controlled firmware dependency lock",
        "migration provenance differs from controlled firmware dependency lock",
    ]
