from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

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


def test_retired_protocol_and_compatibility_runtime_paths_are_rejected() -> None:
    paths = (
        "protocol/rva_control_v1/contract.yaml",
        "protocol/xiaozhi_control_v1/messages.schema.json",
        "firmware/targets/lichuang-dev/README.md",
        "server/apps/realtime_worker/src/realtime_worker/bindings/xiaozhi/protocol.py",
        "server/apps/realtime_worker/src/realtime_worker/bindings/xiaozhi_runtime.py",
    )
    assert VERIFY.validate_tracked_paths(paths) == [
        f"retired runtime path is tracked: {path}" for path in paths
    ]


def test_protocol_contract_is_consistent() -> None:
    assert VERIFY.validate_protocol(ROOT) == []


def test_repository_firmware_composition_is_explicit() -> None:
    assert VERIFY.validate_firmware_composition(ROOT) == []


def test_repository_font_supply_chain_is_complete() -> None:
    assert VERIFY.validate_font_supply_chain(ROOT) == []


def test_font_supply_chain_requires_notice_and_both_licenses(tmp_path: Path) -> None:
    notice = (
        tmp_path
        / "firmware"
        / "components"
        / "ui_font_assets"
        / "THIRD_PARTY_NOTICES.md"
    )
    notice.parent.mkdir(parents=True)
    notice.write_text("missing upstream notice\n", encoding="utf-8")

    assert VERIFY.validate_font_supply_chain(tmp_path) == [
        "Noto Sans CJK copyright notice is missing",
        "font third-party license is missing: "
        "third_party/licenses/Noto-Sans-CJK-OFL-1.1.txt",
        "font third-party license is missing: third_party/licenses/xiaozhi-fonts-MIT.txt",
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
