from __future__ import annotations

import hashlib
import json
import shutil
import struct
import subprocess
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "package-release.ps1"
NOTICE_MANIFEST = SCRIPT.with_name("public_bundle_notices.json")
PRODUCT_ROOT = SCRIPT.parents[2]


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=repository, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _font_image(payload: bytes = b"test-font") -> bytes:
    return struct.pack(
        "<8sHHI32s16s",
        b"RVAFNT1\0",
        1,
        64,
        len(payload),
        hashlib.sha256(payload).digest(),
        b"qwen20-4-v1.6.0",
    ) + payload


def _partition_table() -> bytes:
    definitions = (
        (1, 2, 0x9000, 0x6000, "nvs"),
        (1, 1, 0xF000, 0x1000, "phy_init"),
        (0, 0, 0x10000, 0x400000, "factory"),
        (1, 0x82, 0x410000, 0x80000, "model"),
        (1, 0x40, 0x800000, 0x800000, "font_assets"),
    )
    entries = []
    for type_id, subtype, offset, size, label in definitions:
        entries.append(
            struct.pack(
                "<HBBII16sI",
                0x50AA,
                type_id,
                subtype,
                offset,
                size,
                label.encode().ljust(16, b"\0"),
                0,
            )
        )
    return b"".join(entries) + b"\xff" * (0xC00 - len(entries) * 32)


def _write_build(build: Path) -> None:
    files = {
        "bootloader/bootloader.bin": b"bootloader",
        "partition_table/partition-table.bin": _partition_table(),
        "rva_voice_terminal.bin": b"application",
        "srmodels/srmodels.bin": b"models",
        "font_assets.bin": _font_image(),
    }
    for relative, content in files.items():
        path = build / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    config = [
        'CONFIG_IDF_TARGET="esp32s3"',
        'CONFIG_RVA_DIRECTOR_BOOTSTRAP_URL=""',
        'CONFIG_RVA_DEVICE_BOOTSTRAP_TOKEN=""',
        'CONFIG_RVA_WIFI_PRIMARY_SSID=""',
        'CONFIG_RVA_WIFI_PRIMARY_PASSWORD=""',
        'CONFIG_RVA_WIFI_FALLBACK_SSID=""',
        'CONFIG_RVA_WIFI_FALLBACK_PASSWORD=""',
    ]
    (build / "sdkconfig").write_text("\n".join(config) + "\n", encoding="utf-8")
    flasher = {
        "flash_settings": {"flash_mode": "dio", "flash_size": "16MB", "flash_freq": "80m"},
        "flash_files": {
            "0x0": "bootloader/bootloader.bin",
            "0x8000": "partition_table/partition-table.bin",
            "0x10000": "rva_voice_terminal.bin",
            "0x410000": "srmodels/srmodels.bin",
            "0x800000": "font_assets.bin",
        },
        "extra_esptool_args": {"chip": "esp32s3"},
    }
    (build / "flasher_args.json").write_text(json.dumps(flasher), encoding="utf-8")


def _write_provenance(repository: Path, build: Path, revision: str) -> None:
    artifact_specs = (
        ("bootloader", "0x0", "bootloader/bootloader.bin"),
        ("partition_table", "0x8000", "partition_table/partition-table.bin"),
        ("application", "0x10000", "rva_voice_terminal.bin"),
        ("speech_models", "0x410000", "srmodels/srmodels.bin"),
        ("font_assets", "0x800000", "font_assets.bin"),
    )
    artifacts = []
    for role, offset, relative in artifact_specs:
        path = build / relative
        artifacts.append(
            {
                "role": role,
                "offset": offset,
                "path": relative,
                "included": True,
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    state = {
        "source_revision": revision,
        "tracked_tree_clean": True,
        "worktree_clean": True,
    }
    provenance = {
        "schema_version": 1,
        "source_revision": revision,
        "target": "esp32s3",
        "release_artifacts_requested": True,
        "release_eligible": True,
        "build_start": state,
        "build_end": state,
        "sdkconfig_sha256": _sha256(build / "sdkconfig"),
        "partitions_csv_sha256": _sha256(
            repository / "firmware/apps/voice_terminal/partitions.csv"
        ),
        "flasher_args_sha256": _sha256(build / "flasher_args.json"),
        "font_asset_source": {
            "kind": "explicit_pinned_package",
            "package_sha256": (
                "255868d6e225d08038f38add8f7f2bf2e3567ef7a3b0edcd9703d2101f56e7d5"
            ),
        },
        "artifacts": artifacts,
    }
    (build / "build-provenance.json").write_text(json.dumps(provenance), encoding="utf-8")


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    repository = tmp_path / "product"
    script = repository / "firmware/tools/package-release.ps1"
    script.parent.mkdir(parents=True)
    shutil.copyfile(SCRIPT, script)
    shutil.copyfile(NOTICE_MANIFEST, script.with_name("public_bundle_notices.json"))
    tracked_files = {
        "LICENSE": "Apache License fixture\n",
        "NOTICE": "Product notice fixture\n",
        "firmware/apps/voice_terminal/dependencies.lock": "version: 2.0.0\n",
        "firmware/apps/voice_terminal/partitions.csv": (
            "nvs,data,nvs,0x9000,0x6000\n"
            "phy_init,data,phy,0xf000,0x1000\n"
            "factory,app,factory,0x10000,4M\n"
            "model,data,spiffs,0x410000,0x80000\n"
            "font_assets,data,0x40,0x800000,8M\n"
        ),
        "firmware/components/ui_font_assets/THIRD_PARTY_NOTICES.md": "font notices\n",
        "third_party/licenses/xiaozhi-fonts-MIT.txt": "metadata-only evidence fixture\n",
        ".gitignore": "build/\n*.zip\n",
    }
    for relative, content in tracked_files.items():
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    ofl = repository / "third_party/licenses/Noto-Sans-CJK-OFL-1.1.txt"
    ofl.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(PRODUCT_ROOT / "third_party/licenses/Noto-Sans-CJK-OFL-1.1.txt", ofl)
    _git(repository, "init")
    _git(repository, "config", "user.email", "firmware-test@example.invalid")
    _git(repository, "config", "user.name", "Firmware Test")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "fixture")
    revision = _git(repository, "rev-parse", "HEAD")
    build = repository / "build"
    _write_build(build)
    _write_provenance(repository, build, revision)
    return repository, build, script


def _run(script: Path, build: Path, output: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-File",
            str(script),
            "-BuildDir",
            str(build),
            "-Output",
            str(output),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def test_packages_provenance_bound_public_artifacts_and_notices(tmp_path: Path) -> None:
    _, build, script = _fixture(tmp_path)
    output = build.parent / "public.zip"

    result = _run(script, build, output)

    assert result.returncode == 0, result.stderr
    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == {
            "LICENSE",
            "NOTICE",
            "SHA256SUMS",
            "THIRD_PARTY-Noto-Sans-CJK-OFL-1.1.txt",
            "THIRD_PARTY-font-assets.md",
            "THIRD_PARTY-xiaozhi-fonts-metadata.txt",
            "bootloader.bin",
            "font_assets.bin",
            "manifest.json",
            "partition-table.bin",
            "rva_voice_terminal.bin",
            "srmodels.bin",
        }
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["public_config"] is True
        assert all(Path(item["file"]).name == item["file"] for item in manifest["artifacts"])
        assert len(manifest["notices"]) == 5
        font = next(item for item in manifest["artifacts"] if item["role"] == "font_assets")
        assert font["source_id"] == "qwen20-4-v1.6.0"


def test_rejects_old_binary_even_when_sdkconfig_is_public(tmp_path: Path) -> None:
    _, build, script = _fixture(tmp_path)
    (build / "rva_voice_terminal.bin").write_bytes(b"older-application")

    result = _run(script, build, build.parent / "public.zip")

    assert result.returncode != 0
    assert "application image does not match build provenance" in result.stderr


def test_rejects_forged_source_revision(tmp_path: Path) -> None:
    _, build, script = _fixture(tmp_path)
    provenance_path = build / "build-provenance.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provenance["source_revision"] = "b" * 40
    provenance_path.write_text(json.dumps(provenance), encoding="utf-8")

    result = _run(script, build, build.parent / "public.zip")

    assert result.returncode != 0
    assert "source revision does not match current HEAD" in result.stderr


def test_rejects_dirty_repository(tmp_path: Path) -> None:
    repository, build, script = _fixture(tmp_path)
    (repository / "NOTICE").write_text("modified\n", encoding="utf-8")

    result = _run(script, build, build.parent / "public.zip")

    assert result.returncode != 0
    assert "requires a clean Product worktree" in result.stderr


def test_rejects_non_empty_public_credential(tmp_path: Path) -> None:
    _, build, script = _fixture(tmp_path)
    sdkconfig = build / "sdkconfig"
    sdkconfig.write_text(
        sdkconfig.read_text(encoding="utf-8").replace(
            'CONFIG_RVA_WIFI_PRIMARY_PASSWORD=""',
            'CONFIG_RVA_WIFI_PRIMARY_PASSWORD="not-public"',
        ),
        encoding="utf-8",
    )

    result = _run(script, build, build.parent / "public.zip")

    assert result.returncode != 0
    assert "rejects non-empty CONFIG_RVA_WIFI_PRIMARY_PASSWORD" in result.stderr


@pytest.mark.parametrize("missing", ["srmodels/srmodels.bin", "font_assets.bin"])
def test_rejects_missing_required_partition_image(tmp_path: Path, missing: str) -> None:
    _, build, script = _fixture(tmp_path)
    (build / missing).unlink()

    result = _run(script, build, build.parent / "public.zip")

    assert result.returncode != 0
    assert "Flash artifact not found" in result.stderr


def test_rejects_flasher_manifest_tampering(tmp_path: Path) -> None:
    _, build, script = _fixture(tmp_path)
    flasher_path = build / "flasher_args.json"
    flasher = json.loads(flasher_path.read_text(encoding="utf-8"))
    flasher["flash_files"]["0x10000"] = "../outside.bin"
    flasher_path.write_text(json.dumps(flasher), encoding="utf-8")

    result = _run(script, build, build.parent / "public.zip")

    assert result.returncode != 0
    assert "flasher_args.json does not match build provenance" in result.stderr


def test_rejects_partition_table_tampering(tmp_path: Path) -> None:
    _, build, script = _fixture(tmp_path)
    table = build / "partition_table/partition-table.bin"
    content = bytearray(table.read_bytes())
    content[8] ^= 1
    table.write_bytes(content)

    result = _run(script, build, build.parent / "public.zip")

    assert result.returncode != 0
    assert "partition_table image does not match build provenance" in result.stderr


def test_rejects_locked_license_text_tampering(tmp_path: Path) -> None:
    repository, build, script = _fixture(tmp_path)
    license_path = repository / "third_party/licenses/Noto-Sans-CJK-OFL-1.1.txt"
    license_path.write_text("truncated license\n", encoding="utf-8")
    _git(repository, "add", str(license_path.relative_to(repository)))
    _git(repository, "commit", "-m", "tamper license fixture")
    revision = _git(repository, "rev-parse", "HEAD")
    _write_provenance(repository, build, revision)

    result = _run(script, build, build.parent / "public.zip")

    assert result.returncode != 0
    assert "Release notice digest mismatch" in result.stderr


def test_bundle_is_deterministic_for_identical_inputs(tmp_path: Path) -> None:
    _, build, script = _fixture(tmp_path)
    first = build.parent / "first.zip"
    second = build.parent / "second.zip"

    first_result = _run(script, build, first)
    second_result = _run(script, build, second)

    assert first_result.returncode == 0, first_result.stderr
    assert second_result.returncode == 0, second_result.stderr
    assert hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(second.read_bytes()).digest()
