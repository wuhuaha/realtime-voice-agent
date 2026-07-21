from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "verify_repository", ROOT / "scripts" / "verify_repository.py"
)
assert SPEC is not None and SPEC.loader is not None
VERIFY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY)


def write_boundary_decision(root: Path) -> None:
    research_repository = "voice-agent" + "-research"
    decision = root / "docs" / "decisions" / "0004-research-production-boundary.md"
    decision.parent.mkdir(parents=True)
    decision.write_text(
        "\n".join(
            (
                "唯一 authoring source",
                f"不得读取 `{research_repository}`",
                "manifest不是运行配置",
            )
        ),
        encoding="utf-8",
    )


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


def test_legacy_rollback_provenance_is_optional(tmp_path: Path) -> None:
    assert VERIFY.validate_optional_legacy_rollback(tmp_path) == []


def test_retained_legacy_rollback_requires_complete_provenance(tmp_path: Path) -> None:
    (tmp_path / VERIFY.LEGACY_FIRMWARE_PATH).mkdir(parents=True)
    assert VERIFY.validate_optional_legacy_rollback(tmp_path) == [
        "missing manifest: migration/baseline/source-manifest.yaml",
        "firmware dependency lock identity inputs are incomplete",
    ]


def test_repository_files_include_untracked_but_not_ignored_sources(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (tmp_path / "tracked.py").write_text("tracked\n", encoding="utf-8")
    (tmp_path / "untracked.cc").write_text("untracked\n", encoding="utf-8")
    (tmp_path / "ignored.py").write_text("ignored\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore", "tracked.py"], cwd=tmp_path, check=True)

    assert set(VERIFY.repository_files(tmp_path)) == {".gitignore", "tracked.py", "untracked.cc"}


def test_research_boundary_rejects_yaml_reference(tmp_path: Path) -> None:
    write_boundary_decision(tmp_path)
    config = tmp_path / "deploy" / "service.yaml"
    config.parent.mkdir(parents=True)
    research_repository = "realtime-voice-agent" + "-research"
    config.write_text(f"source: {research_repository}\n", encoding="utf-8")

    assert VERIFY.validate_research_boundary(tmp_path, ("deploy/service.yaml",)) == [
        "Product executable/config references Research repository: deploy/service.yaml"
    ]


def test_research_boundary_rejects_runtime_and_build_carrier_references(tmp_path: Path) -> None:
    write_boundary_decision(tmp_path)
    research_repository = "realtime-voice-agent" + "-research"
    carrier_paths = (
        "firmware/overlay/feature.patch",
        "firmware/src/runtime.cc",
        "firmware/include/runtime.h",
        "firmware/.env.local.example",
        "firmware/sdkconfig.defaults",
        "deploy/service.ini",
        "Makefile",
    )
    for raw_path in carrier_paths:
        path = tmp_path / raw_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"source={research_repository}\n", encoding="utf-8")

    assert VERIFY.validate_research_boundary(tmp_path, carrier_paths) == [
        f"Product executable/config references Research repository: {raw_path}"
        for raw_path in carrier_paths
    ]


def test_research_boundary_scan_covers_executable_and_config_formats() -> None:
    expected_scanned = (
        "runtime.c",
        "runtime.cc",
        "runtime.cpp",
        "runtime.cxx",
        "runtime.h",
        "runtime.hh",
        "runtime.hpp",
        "runtime.hxx",
        "feature.patch",
        "build.cmake",
        "build.ps1",
        "build.py",
        "config.toml",
        "config.ini",
        "config.cfg",
        "config.conf",
        "config.properties",
        "config.yml",
        "config.yaml",
        "config.json",
        "sdkconfig.defaults",
        ".env.example",
        ".env.local.example",
        "entrypoint.sh",
        "install.bat",
        "install.cmd",
        "CMakeLists.txt",
        "Dockerfile",
        "Dockerfile.release",
        "Makefile",
        "GNUmakefile",
        "Procfile",
    )

    assert all(VERIFY.is_executable_or_config(Path(name)) for name in expected_scanned)
    assert not VERIFY.is_executable_or_config(Path("README.md"))
    assert not VERIFY.is_executable_or_config(Path("firmware.bin"))


def test_research_boundary_scans_bom_marked_utf16(tmp_path: Path) -> None:
    write_boundary_decision(tmp_path)
    config = tmp_path / "deploy" / "windows.ps1"
    config.parent.mkdir(parents=True)
    research_repository = "voice-agent" + "-research"
    config.write_text(f'$ResearchRepo = "{research_repository}"\n', encoding="utf-16")

    assert VERIFY.validate_research_boundary(tmp_path, ("deploy/windows.ps1",)) == [
        "Product executable/config references Research repository: deploy/windows.ps1"
    ]


def test_research_boundary_reports_unsupported_text_encoding_and_ignores_binary(
    tmp_path: Path,
) -> None:
    write_boundary_decision(tmp_path)
    config = tmp_path / "deploy" / "service.ini"
    config.parent.mkdir(parents=True)
    config.write_bytes(b"\x80\x81\x82")
    no_bom_utf16 = tmp_path / "deploy" / "windows-no-bom.ps1"
    research_repository = "voice-agent" + "-research"
    no_bom_utf16.write_bytes(research_repository.encode("utf-16-le"))
    binary = tmp_path / "deploy" / "firmware.bin"
    binary.write_bytes(b"\x80\x81\x82")

    assert VERIFY.validate_research_boundary(
        tmp_path,
        (
            "deploy/service.ini",
            "deploy/windows-no-bom.ps1",
            "deploy/firmware.bin",
        ),
    ) == [
        "Product executable/config cannot be boundary-scanned: deploy/service.ini: "
        "expected UTF-8 or BOM-marked UTF-16 text",
        "Product executable/config cannot be boundary-scanned: deploy/windows-no-bom.ps1: "
        "expected UTF-8 or BOM-marked UTF-16 text",
    ]


def test_research_boundary_exempts_only_the_evidence_source_manifest(tmp_path: Path) -> None:
    write_boundary_decision(tmp_path)
    research_repository = "voice-agent" + "-research"
    evidence = tmp_path / "migration" / "baseline" / "source-manifest.yaml"
    runtime = tmp_path / "deploy" / "source-manifest.yaml"
    evidence.parent.mkdir(parents=True)
    runtime.parent.mkdir(parents=True)
    evidence.write_text(f"source_repository: {research_repository}\n", encoding="utf-8")
    runtime.write_text(f"source_repository: {research_repository}\n", encoding="utf-8")

    assert VERIFY.validate_research_boundary(
        tmp_path,
        (
            "migration/baseline/source-manifest.yaml",
            "deploy/source-manifest.yaml",
        ),
    ) == [
        "Product executable/config references Research repository: deploy/source-manifest.yaml"
    ]


def test_research_boundary_allows_safe_config_and_self_verifier(tmp_path: Path) -> None:
    write_boundary_decision(tmp_path)
    config = tmp_path / "deploy" / "service.json"
    config.parent.mkdir(parents=True)
    config.write_text('{"service": "realtime-voice-agent"}\n', encoding="utf-8")
    verifier = tmp_path / "scripts" / "verify_repository.py"
    verifier.parent.mkdir(parents=True)
    research_repository = "voice-agent" + "-research"
    verifier.write_text(f'FORBIDDEN = "{research_repository}"\n', encoding="utf-8")

    assert VERIFY.validate_research_boundary(
        tmp_path,
        ("deploy/service.json", "scripts/verify_repository.py"),
    ) == []


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
