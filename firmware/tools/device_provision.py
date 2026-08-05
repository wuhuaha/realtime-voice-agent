#!/usr/bin/env python3
"""Safely provision the configuration NVS partition of an RVA ESP32-S3 device."""

from __future__ import annotations

import argparse
import csv
import getpass
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


class ProvisioningError(RuntimeError):
    """A user-actionable provisioning failure."""


@dataclass(frozen=True)
class Partition:
    offset: int
    size: int


@dataclass(frozen=True)
class DeviceConfig:
    ssid: str
    password: str
    bootstrap_url: str
    bootstrap_token: str
    token_origin: str


@dataclass(frozen=True)
class FlashArtifact:
    role: str
    offset: int
    file: str
    sha256: str


PROVISIONING_PARTITION = Partition(offset=0x9000, size=0x6000)
FLASH_LAYOUT: dict[str, tuple[int, str, int]] = {
    "bootloader": (0x0, "bootloader.bin", 0x8000),
    "partition_table": (0x8000, "partition-table.bin", 0x1000),
    "application": (0x10000, "rva_voice_terminal.bin", 0x400000),
    "speech_models": (0x410000, "srmodels.bin", 0x80000),
    "font_assets": (0x800000, "font_assets.bin", 0x800000),
}
MAX_BOOTSTRAP_TOKEN_BYTES = 3967


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProvisioningError(f"{name} must be a JSON object")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ProvisioningError(f"{name} must be a string")
    return value


def _parse_integer(value: Any, name: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool):
        raise ProvisioningError(f"{name} must be an integer or 0x-prefixed string")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str):
        try:
            result = int(value, 0)
        except ValueError as error:
            raise ProvisioningError(f"{name} must be an integer or 0x-prefixed string") from error
    else:
        raise ProvisioningError(f"{name} must be an integer or 0x-prefixed string")
    if result < 0 or (result == 0 and not allow_zero):
        raise ProvisioningError(f"{name} must be {'non-negative' if allow_zero else 'positive'}")
    return result


def load_partition(bundle: Path) -> Partition:
    if bundle.is_dir():
        manifest_path = bundle / "manifest.json"
        if not manifest_path.is_file():
            raise ProvisioningError("bundle directory is missing manifest.json")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ProvisioningError("bundle manifest is unreadable or invalid JSON") from error
    elif bundle.is_file():
        try:
            with zipfile.ZipFile(bundle) as archive:
                names = archive.namelist()
                if names.count("manifest.json") != 1:
                    raise ProvisioningError("bundle zip must contain one top-level manifest.json")
                manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        except ProvisioningError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, zipfile.BadZipFile, KeyError) as error:
            raise ProvisioningError("bundle zip or manifest is unreadable") from error
    else:
        raise ProvisioningError("bundle must be a release zip or extracted bundle directory")

    root = _object(manifest, "manifest")
    if root.get("target") != "esp32s3":
        raise ProvisioningError("bundle target must be esp32s3")
    if "provisioning" not in root:
        raise ProvisioningError("bundle manifest schema missing provisioning metadata")
    provisioning = _object(root["provisioning"], "manifest.provisioning")
    if provisioning.get("schema_version") != 1:
        raise ProvisioningError("unsupported manifest provisioning schema_version")
    partition = _object(provisioning.get("partition"), "manifest.provisioning.partition")
    if partition.get("label") != "nvs":
        raise ProvisioningError("provisioning partition label must be nvs")
    offset = _parse_integer(partition.get("offset"), "provisioning partition offset")
    size = _parse_integer(partition.get("size"), "provisioning partition size")
    if offset % 0x1000 or size % 0x1000:
        raise ProvisioningError("provisioning partition offset and size must be 4 KiB aligned")
    parsed = Partition(offset=offset, size=size)
    if parsed != PROVISIONING_PARTITION:
        raise ProvisioningError("provisioning schema v1 requires NVS at 0x9000 with size 0x6000")
    return parsed


def _bundle_files(bundle: Path) -> dict[str, bytes]:
    if bundle.is_dir():
        files: dict[str, bytes] = {}
        for path in bundle.rglob("*"):
            if not path.is_file():
                continue
            if path.parent != bundle:
                raise ProvisioningError("bundle directory must contain only top-level files")
            files[path.name] = path.read_bytes()
        return files
    if bundle.is_file():
        try:
            with zipfile.ZipFile(bundle) as archive:
                names = archive.namelist()
                if any(name != Path(name).name or name.endswith("/") for name in names):
                    raise ProvisioningError("bundle zip must contain only top-level files")
                if len(names) != len(set(names)):
                    raise ProvisioningError("bundle zip contains duplicate filenames")
                return {name: archive.read(name) for name in names}
        except ProvisioningError:
            raise
        except (OSError, zipfile.BadZipFile, KeyError) as error:
            raise ProvisioningError("bundle zip is unreadable") from error
    raise ProvisioningError("bundle must be a release zip or extracted bundle directory")


def load_flash_artifacts(bundle: Path) -> tuple[dict[str, bytes], list[FlashArtifact]]:
    files = _bundle_files(bundle)
    try:
        checksum_text = files["SHA256SUMS"].decode("utf-8")
        manifest = _object(json.loads(files["manifest.json"].decode("utf-8")), "manifest")
    except KeyError as error:
        raise ProvisioningError("bundle is missing manifest.json or SHA256SUMS") from error
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ProvisioningError("bundle manifest or SHA256SUMS is invalid") from error
    expected_checksums: dict[str, str] = {}
    for line in checksum_text.splitlines():
        parts = line.split("  ", 1)
        if len(parts) != 2 or len(parts[0]) != 64 or any(character not in "0123456789abcdef" for character in parts[0]):
            raise ProvisioningError("SHA256SUMS contains an invalid entry")
        name = parts[1]
        if name != Path(name).name or name == "SHA256SUMS" or name in expected_checksums:
            raise ProvisioningError("SHA256SUMS contains an unsafe or duplicate filename")
        expected_checksums[name] = parts[0]
    if set(expected_checksums) != set(files) - {"SHA256SUMS"}:
        raise ProvisioningError("SHA256SUMS does not cover every bundle file exactly once")
    for name, expected in expected_checksums.items():
        if hashlib.sha256(files[name]).hexdigest() != expected:
            raise ProvisioningError(f"bundle checksum mismatch for {name}")
    if manifest.get("target") != "esp32s3":
        raise ProvisioningError("bundle target must be esp32s3")
    records = manifest.get("artifacts")
    if not isinstance(records, list):
        raise ProvisioningError("manifest.artifacts must be an array")
    required_roles = set(FLASH_LAYOUT)
    artifacts: list[FlashArtifact] = []
    roles: set[str] = set()
    offsets: set[int] = set()
    for index, value in enumerate(records):
        record = _object(value, f"manifest.artifacts[{index}]")
        role = _text(record.get("role"), f"manifest.artifacts[{index}].role")
        filename = _text(record.get("file"), f"manifest.artifacts[{index}].file")
        digest = _text(record.get("sha256"), f"manifest.artifacts[{index}].sha256")
        offset = _parse_integer(record.get("offset"), f"manifest.artifacts[{index}].offset", allow_zero=True)
        if role not in required_roles or role in roles or offset in offsets:
            raise ProvisioningError("manifest contains an unexpected or duplicate flash artifact")
        expected_offset, expected_filename, max_size = FLASH_LAYOUT[role]
        if offset != expected_offset or filename != expected_filename:
            raise ProvisioningError(f"manifest {role} artifact does not match the schema v1 flash layout")
        if filename != Path(filename).name or filename not in files:
            raise ProvisioningError("manifest flash artifact filename is missing or unsafe")
        if not 0 < len(files[filename]) <= max_size:
            raise ProvisioningError(f"manifest {role} artifact exceeds its schema v1 partition capacity")
        if len(digest) != 64 or digest != hashlib.sha256(files[filename]).hexdigest():
            raise ProvisioningError(f"manifest digest mismatch for {filename}")
        if record.get("included") is not True:
            raise ProvisioningError(f"manifest flash artifact is not included: {filename}")
        artifacts.append(FlashArtifact(role, offset, filename, digest))
        roles.add(role)
        offsets.add(offset)
    if roles != required_roles:
        raise ProvisioningError("manifest must declare exactly the five public firmware artifacts")
    return files, sorted(artifacts, key=lambda artifact: artifact.offset)


def validate_bundle(bundle: Path) -> tuple[Partition, dict[str, bytes], list[FlashArtifact]]:
    partition = load_partition(bundle)
    files, artifacts = load_flash_artifacts(bundle)
    if partition.offset + partition.size > 0x1000000:
        raise ProvisioningError("provisioning partition exceeds the 16 MiB target flash")
    for artifact in artifacts:
        size = len(files[artifact.file])
        if artifact.offset < partition.offset + partition.size and partition.offset < artifact.offset + size:
            raise ProvisioningError("provisioning partition overlaps a public firmware artifact")
    return partition, files, artifacts


def _normalized_origin(bootstrap_url: str) -> str:
    if len(bootstrap_url.encode("utf-8")) > 255 or any(ord(character) <= 0x20 for character in bootstrap_url):
        raise ProvisioningError("endpoint must be at most 255 bytes and contain no spaces or controls")
    try:
        parsed = urlsplit(bootstrap_url)
        port = parsed.port
    except ValueError as error:
        raise ProvisioningError("endpoint has an invalid authority or port") from error
    if parsed.scheme not in {"http", "https"}:
        raise ProvisioningError("endpoint scheme must be lowercase http or https")
    if parsed.username is not None or parsed.password is not None or not parsed.hostname:
        raise ProvisioningError("endpoint must have a host and no userinfo")
    if parsed.path != "/v1/session/bootstrap" or parsed.query or parsed.fragment:
        raise ProvisioningError("endpoint path must be exactly /v1/session/bootstrap with no query or fragment")
    host = parsed.hostname.lower()
    if any(character not in "abcdefghijklmnopqrstuvwxyz0123456789.-_:" for character in host):
        raise ProvisioningError("endpoint host contains unsupported characters")
    if ":" in host:
        host = f"[{host}]"
    elif not any(character.isascii() and character.isalnum() for character in host):
        raise ProvisioningError("endpoint host must contain at least one ASCII letter or digit")
    resolved_port = port or (443 if parsed.scheme == "https" else 80)
    if not 1 <= resolved_port <= 65535:
        raise ProvisioningError("endpoint port is outside 1..65535")
    return f"{parsed.scheme}://{host}:{resolved_port}"


def validate_config(config: DeviceConfig) -> DeviceConfig:
    ssid_bytes = config.ssid.encode("utf-8")
    password_bytes = config.password.encode("utf-8")
    token_bytes = config.bootstrap_token.encode("utf-8")
    if not 1 <= len(ssid_bytes) <= 32 or "\0" in config.ssid:
        raise ProvisioningError("wifi.ssid must be 1..32 UTF-8 bytes and contain no NUL")
    if len(password_bytes) > 64 or (password_bytes and len(password_bytes) < 8) or "\0" in config.password:
        raise ProvisioningError("wifi.password must be empty or 8..64 UTF-8 bytes and contain no NUL")
    if not 1 <= len(token_bytes) <= MAX_BOOTSTRAP_TOKEN_BYTES or any(
        character in config.bootstrap_token for character in "\0\r\n"
    ):
        raise ProvisioningError(
            f"endpoint.bootstrap_token must be 1..{MAX_BOOTSTRAP_TOKEN_BYTES} bytes and contain no NUL or newline"
        )
    normalized_origin = _normalized_origin(config.bootstrap_url)
    return DeviceConfig(
        ssid=config.ssid,
        password=config.password,
        bootstrap_url=config.bootstrap_url,
        bootstrap_token=config.bootstrap_token,
        token_origin=normalized_origin,
    )


def _config_from_mapping(value: Any) -> DeviceConfig:
    root = _object(value, "config")
    if set(root) != {"schema_version", "wifi", "endpoint"} or root.get("schema_version") != 1:
        raise ProvisioningError("config must contain only schema_version=1, wifi, and endpoint")
    wifi = _object(root["wifi"], "config.wifi")
    endpoint = _object(root["endpoint"], "config.endpoint")
    if set(wifi) != {"ssid", "password"}:
        raise ProvisioningError("wifi must contain exactly ssid and password")
    if set(endpoint) != {"bootstrap_url", "bootstrap_token"}:
        raise ProvisioningError("endpoint must contain exactly bootstrap_url and bootstrap_token")
    return validate_config(
        DeviceConfig(
            ssid=_text(wifi["ssid"], "wifi.ssid"),
            password=_text(wifi["password"], "wifi.password"),
            bootstrap_url=_text(endpoint["bootstrap_url"], "endpoint.bootstrap_url"),
            bootstrap_token=_text(endpoint["bootstrap_token"], "endpoint.bootstrap_token"),
            token_origin="",
        )
    )


def _assert_private_file(path: Path) -> None:
    if not path.is_file():
        raise ProvisioningError("config path must be a regular file")
    if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ProvisioningError("config file permissions must not grant group or other access")


def load_config(path: Path | None) -> DeviceConfig:
    if path is None:
        return validate_config(
            DeviceConfig(
                ssid=getpass.getpass("Wi-Fi SSID: "),
                password=getpass.getpass("Wi-Fi password (empty for open network): "),
                bootstrap_url=getpass.getpass("Director bootstrap URL: "),
                bootstrap_token=getpass.getpass("Bootstrap token: "),
                token_origin="",
            )
        )
    _assert_private_file(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ProvisioningError("config file is unreadable or invalid JSON") from error
    return _config_from_mapping(value)


def write_nvs_csv(path: Path, config: DeviceConfig) -> None:
    # Values are handled by csv.writer so commas and quotes never alter the NVS schema.
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(("key", "type", "encoding", "value"))
        writer.writerow(("rva_wifi", "namespace", "", ""))
        writer.writerow(("ssid", "data", "string", config.ssid))
        writer.writerow(("password", "data", "string", config.password))
        writer.writerow(("voice_agent", "namespace", "", ""))
        writer.writerow(("ws_url", "data", "string", config.bootstrap_url))
        writer.writerow(("token_origin", "data", "string", config.token_origin))
        writer.writerow(("token", "data", "string", config.bootstrap_token))


def find_nvs_generator(idf_path: Path | None) -> Path:
    root = idf_path or (Path(os.environ["IDF_PATH"]) if os.environ.get("IDF_PATH") else None)
    if root is None:
        raise ProvisioningError("ESP-IDF is unavailable; set IDF_PATH or pass --idf-path")
    script = root / "components" / "nvs_flash" / "nvs_partition_generator" / "nvs_partition_gen.py"
    if not script.is_file():
        raise ProvisioningError("official ESP-IDF nvs_partition_gen.py was not found")
    return script


def find_idf_python(explicit: Path | None) -> Path:
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit)
    environment = os.environ.get("IDF_PYTHON_ENV_PATH")
    if environment:
        root = Path(environment)
        candidates.extend((root / "Scripts" / "python.exe", root / "bin" / "python"))
    candidates.append(Path(sys.executable))
    for candidate in candidates:
        if not candidate.is_file():
            continue
        result = subprocess.run(
            (str(candidate), "-c", "import esp_idf_nvs_partition_gen"),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return candidate.resolve()
    raise ProvisioningError(
        "ESP-IDF Python environment is unavailable; activate ESP-IDF, set IDF_PYTHON_ENV_PATH, or pass --idf-python"
    )


def _run(command: Sequence[str], description: str) -> None:
    try:
        subprocess.run(command, check=True)
    except FileNotFoundError as error:
        raise ProvisioningError(f"{description} executable was not found") from error
    except subprocess.CalledProcessError as error:
        raise ProvisioningError(f"{description} failed with exit code {error.returncode}") from error


def generate_image(
    config: DeviceConfig,
    partition: Partition,
    directory: Path,
    idf_path: Path | None,
    idf_python: Path | None = None,
) -> Path:
    csv_path = directory / "device-config.csv"
    image_path = directory / "device-config.bin"
    write_nvs_csv(csv_path, config)
    generator = find_nvs_generator(idf_path)
    interpreter = find_idf_python(idf_python)
    _run(
        (str(interpreter), str(generator), "generate", str(csv_path), str(image_path), hex(partition.size)),
        "NVS generator",
    )
    if not image_path.is_file() or image_path.stat().st_size != partition.size:
        raise ProvisioningError("NVS generator did not produce an image matching the partition size")
    return image_path


def esptool_command(port: str, *arguments: str) -> tuple[str, ...]:
    if not port or any(character.isspace() for character in port):
        raise ProvisioningError("--port must be a non-empty serial port without whitespace")
    return (sys.executable, "-m", "esptool", "--chip", "esp32s3", "--port", port, *arguments)


def provision(
    bundle: Path,
    config_path: Path | None,
    port: str | None,
    idf_path: Path | None,
    idf_python: Path | None,
    dry_run: bool,
) -> None:
    partition, _, _ = validate_bundle(bundle)
    config = load_config(config_path)
    if dry_run:
        print(
            f"Validation passed; would write configuration partition at "
            f"{partition.offset:#x} ({partition.size} bytes)."
        )
        return
    if port is None:
        raise ProvisioningError("--port is required unless --dry-run is used")
    with tempfile.TemporaryDirectory(prefix="rva-provision-") as temporary:
        image = generate_image(config, partition, Path(temporary), idf_path, idf_python)
        _run(esptool_command(port, "write_flash", hex(partition.offset), str(image)), "esptool write_flash")
        readback = Path(temporary) / "device-config-readback.bin"
        _run(
            esptool_command(port, "read_flash", hex(partition.offset), hex(partition.size), str(readback)),
            "esptool read_flash",
        )
        if not readback.is_file() or (
            hashlib.sha256(readback.read_bytes()).digest() != hashlib.sha256(image.read_bytes()).digest()
        ):
            raise ProvisioningError("configuration read-back verification failed")
    print("Device configuration provisioned successfully; secrets were not displayed.")


def erase_config(bundle: Path, port: str | None, dry_run: bool) -> None:
    partition, _, _ = validate_bundle(bundle)
    if dry_run:
        print(
            f"Validation passed; would erase configuration partition at "
            f"{partition.offset:#x} ({partition.size} bytes)."
        )
        return
    if port is None:
        raise ProvisioningError("--port is required unless --dry-run is used")
    _run(esptool_command(port, "erase_region", hex(partition.offset), hex(partition.size)), "esptool erase_region")
    print("Device configuration partition erased successfully.")


def flash(bundle: Path, port: str | None, dry_run: bool) -> None:
    _, files, artifacts = validate_bundle(bundle)
    summary = ", ".join(f"{artifact.role}@{artifact.offset:#x}" for artifact in artifacts)
    if dry_run:
        print(f"Bundle validation passed; would flash {summary}. Configuration NVS is preserved.")
        return
    with tempfile.TemporaryDirectory(prefix="rva-flash-") as temporary:
        directory = Path(temporary)
        arguments: list[str] = ["write_flash"]
        for artifact in artifacts:
            path = directory / artifact.file
            path.write_bytes(files[artifact.file])
            arguments.extend((hex(artifact.offset), str(path)))
        _run(esptool_command(port, *arguments), "esptool write_flash")
    print("Public firmware flashed successfully; configuration NVS was preserved.")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate", help="validate bundle and private configuration")
    validate_parser.add_argument("--bundle", required=True, type=Path)
    validate_parser.add_argument("--config", type=Path, help="optional private JSON configuration")

    provision_parser = subparsers.add_parser("provision", help="write only the configuration NVS partition")
    provision_parser.add_argument("--bundle", required=True, type=Path)
    provision_parser.add_argument("--config", type=Path, help="private JSON file; omit for hidden interactive input")
    provision_parser.add_argument("--port", help="ESP32-S3 serial port")
    provision_parser.add_argument("--idf-path", type=Path, help="ESP-IDF root containing the official NVS generator")
    provision_parser.add_argument(
        "--idf-python",
        type=Path,
        help="Python executable from the ESP-IDF environment; auto-detected when activated",
    )
    provision_parser.add_argument("--dry-run", action="store_true")

    erase_parser = subparsers.add_parser("erase-config", help="erase only the configuration NVS partition")
    erase_parser.add_argument("--bundle", required=True, type=Path)
    erase_parser.add_argument("--port", help="ESP32-S3 serial port")
    erase_parser.add_argument("--dry-run", action="store_true")

    flash_parser = subparsers.add_parser("flash", help="flash the five public firmware artifacts and preserve NVS")
    flash_parser.add_argument("--bundle", required=True, type=Path)
    flash_parser.add_argument("--port", help="ESP32-S3 serial port; required unless --dry-run is used")
    flash_parser.add_argument("--dry-run", action="store_true")
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    try:
        parsed = _parser().parse_args(arguments)
        if parsed.command == "validate":
            partition, _, _ = validate_bundle(parsed.bundle)
            if parsed.config is not None:
                load_config(parsed.config)
                suffix = " and configuration"
            else:
                suffix = ""
            print(f"Bundle{suffix} is valid for NVS at {partition.offset:#x} ({partition.size} bytes).")
        elif parsed.command == "provision":
            provision(parsed.bundle, parsed.config, parsed.port, parsed.idf_path, parsed.idf_python, parsed.dry_run)
        elif parsed.command == "erase-config":
            erase_config(parsed.bundle, parsed.port, parsed.dry_run)
        else:
            if parsed.port is None and not parsed.dry_run:
                raise ProvisioningError("--port is required unless --dry-run is used")
            flash(parsed.bundle, parsed.port, parsed.dry_run)
        return 0
    except ProvisioningError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
