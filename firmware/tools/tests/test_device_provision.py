from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "device_provision.py"
SPEC = importlib.util.spec_from_file_location("device_provision", SCRIPT)
assert SPEC and SPEC.loader
device_provision = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = device_provision
SPEC.loader.exec_module(device_provision)


def _manifest() -> dict[str, object]:
    definitions = (
        ("bootloader", "0x0", "bootloader.bin"),
        ("partition_table", "0x8000", "partition-table.bin"),
        ("application", "0x10000", "rva_voice_terminal.bin"),
        ("speech_models", "0x410000", "srmodels.bin"),
        ("font_assets", "0x800000", "font_assets.bin"),
    )
    artifacts = []
    for role, offset, filename in definitions:
        payload = f"payload:{role}".encode()
        artifacts.append(
            {
                "role": role,
                "offset": offset,
                "file": filename,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "included": True,
            }
        )
    return {
        "schema_version": 1,
        "target": "esp32s3",
        "provisioning": {
            "schema_version": 1,
            "partition": {"label": "nvs", "offset": "0x9000", "size": "0x6000"},
        },
        "artifacts": artifacts,
    }


def _bundle(tmp_path: Path, *, mutate: object | None = None) -> Path:
    manifest = _manifest()
    if mutate:
        mutate(manifest)
    files: dict[str, bytes] = {"manifest.json": json.dumps(manifest).encode()}
    for artifact in manifest.get("artifacts", []):
        files[artifact["file"]] = f"payload:{artifact['role']}".encode()
    files["NOTICE.txt"] = b"notice"
    sums = "\n".join(f"{hashlib.sha256(payload).hexdigest()}  {name}" for name, payload in sorted(files.items()))
    files["SHA256SUMS"] = (sums + "\n").encode()
    path = tmp_path / "bundle.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
    return path


def _config(tmp_path: Path, **overrides: str) -> Path:
    endpoint = {
        "bootstrap_url": "https://Voice.Example/v1/session/bootstrap",
        "bootstrap_token": "secret-bootstrap-token",
    }
    wifi = {"ssid": "测试网络", "password": "password123"}
    for name, value in overrides.items():
        target, field = name.split("__", 1)
        (wifi if target == "wifi" else endpoint)[field] = value
    path = tmp_path / "private.json"
    path.write_text(json.dumps({"schema_version": 1, "wifi": wifi, "endpoint": endpoint}), encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
    return path


def test_validate_zip_and_config_without_disclosing_secrets(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = _config(tmp_path)
    assert device_provision.main(["validate", "--bundle", str(_bundle(tmp_path)), "--config", str(config)]) == 0
    output = capsys.readouterr()
    assert "0x9000" in output.out
    assert "secret-bootstrap-token" not in output.out + output.err
    assert "password123" not in output.out + output.err


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"wifi__ssid": "x" * 33}, "1..32"),
        ({"wifi__password": "short"}, "8..64"),
        ({"endpoint__bootstrap_url": "https://voice.example/v1/session/bootstrap?x=1"}, "exactly"),
        ({"endpoint__bootstrap_url": "wss://voice.example/v1/session/bootstrap"}, "http or https"),
        ({"endpoint__bootstrap_url": "https://___/v1/session/bootstrap"}, "ASCII letter or digit"),
        ({"endpoint__bootstrap_token": "x" * 3968}, "1..3967"),
    ],
)
def test_rejects_invalid_configuration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], overrides: dict[str, str], message: str
) -> None:
    result = device_provision.main(
        ["validate", "--bundle", str(_bundle(tmp_path)), "--config", str(_config(tmp_path, **overrides))]
    )
    assert result == 2
    assert message in capsys.readouterr().err


def test_reports_missing_provisioning_schema(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bundle = _bundle(tmp_path, mutate=lambda manifest: manifest.pop("provisioning"))
    assert device_provision.main(["validate", "--bundle", str(bundle), "--config", str(_config(tmp_path))]) == 2
    assert "schema missing" in capsys.readouterr().err


def test_rejects_noncanonical_provisioning_region(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def mutate(manifest: dict[str, object]) -> None:
        manifest["provisioning"]["partition"]["offset"] = "0x20000"

    assert device_provision.main(["validate", "--bundle", str(_bundle(tmp_path, mutate=mutate))]) == 2
    assert "requires NVS at 0x9000" in capsys.readouterr().err


def test_rejects_noncanonical_flash_offset(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def mutate(manifest: dict[str, object]) -> None:
        manifest["artifacts"][0]["offset"] = "0x2000"

    assert device_provision.main(["validate", "--bundle", str(_bundle(tmp_path, mutate=mutate))]) == 2
    assert "schema v1 flash layout" in capsys.readouterr().err


def test_validate_bundle_without_config_never_prompts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(device_provision.getpass, "getpass", lambda prompt: pytest.fail("unexpected prompt"))
    assert device_provision.main(["validate", "--bundle", str(_bundle(tmp_path))]) == 0


def test_rejects_user_supplied_token_origin(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = json.loads(_config(tmp_path).read_text(encoding="utf-8"))
    config["endpoint"]["token_origin"] = "https://voice.example:443"
    path = tmp_path / "with-origin.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
    assert device_provision.main(["validate", "--bundle", str(_bundle(tmp_path)), "--config", str(path)]) == 2
    assert "exactly bootstrap_url and bootstrap_token" in capsys.readouterr().err


def test_dry_run_requires_no_port_or_toolchain(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    result = device_provision.main(
        ["provision", "--bundle", str(_bundle(tmp_path)), "--config", str(_config(tmp_path)), "--dry-run"]
    )
    assert result == 0
    assert "would write" in capsys.readouterr().out


def test_nvs_csv_uses_runtime_namespaces_and_keys(tmp_path: Path) -> None:
    config = device_provision.load_config(_config(tmp_path))
    output = tmp_path / "config.csv"
    device_provision.write_nvs_csv(output, config)
    text = output.read_text(encoding="utf-8")
    assert "rva_wifi,namespace" in text
    assert "voice_agent,namespace" in text
    assert "ws_url,data,string,https://Voice.Example/v1/session/bootstrap" in text
    assert "token_origin,data,string,https://voice.example:443" in text


def test_generate_image_invokes_official_generator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    idf = tmp_path / "idf"
    generator = idf / "components/nvs_flash/nvs_partition_generator/nvs_partition_gen.py"
    generator.parent.mkdir(parents=True)
    generator.write_text(
        "import pathlib, sys\n"
        "assert sys.argv[1] == 'generate'\n"
        "pathlib.Path(sys.argv[3]).write_bytes(b'\\xff' * int(sys.argv[4], 0))\n",
        encoding="utf-8",
    )
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    monkeypatch.setattr(device_provision, "find_idf_python", lambda explicit: Path(sys.executable))
    image = device_provision.generate_image(
        device_provision.load_config(_config(tmp_path)),
        device_provision.Partition(0x9000, 0x6000),
        temporary,
        idf,
        Path(sys.executable),
    )
    assert image.stat().st_size == 0x6000


def test_provision_writes_only_nvs_and_temp_files_are_removed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    generated_directories: list[Path] = []
    commands: list[tuple[str, ...]] = []

    def fake_generate(
        config: object, partition: object, directory: Path, idf_path: object, idf_python: object
    ) -> Path:
        generated_directories.append(directory)
        image = directory / "device-config.bin"
        image.write_bytes(b"image")
        return image

    def fake_run(command: tuple[str, ...], description: str) -> None:
        commands.append(tuple(command))
        if "read_flash" in command:
            Path(command[-1]).write_bytes(generated_directories[0].joinpath("device-config.bin").read_bytes())

    monkeypatch.setattr(device_provision, "generate_image", fake_generate)
    monkeypatch.setattr(device_provision, "_run", fake_run)
    device_provision.provision(_bundle(tmp_path), _config(tmp_path), "COM14", None, None, False)
    assert len(commands) == 2
    assert "write_flash" in commands[0]
    assert "0x9000" in commands[0]
    assert "read_flash" in commands[1]
    assert all(not directory.exists() for directory in generated_directories)


def test_provision_fails_when_readback_does_not_match(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_generate(
        config: object, partition: object, directory: Path, idf_path: object, idf_python: object
    ) -> Path:
        image = directory / "device-config.bin"
        image.write_bytes(b"expected")
        return image

    def fake_run(command: tuple[str, ...], description: str) -> None:
        if "read_flash" in command:
            Path(command[-1]).write_bytes(b"different")

    monkeypatch.setattr(device_provision, "generate_image", fake_generate)
    monkeypatch.setattr(device_provision, "_run", fake_run)
    with pytest.raises(device_provision.ProvisioningError, match="read-back verification failed"):
        device_provision.provision(_bundle(tmp_path), _config(tmp_path), "COM14", None, None, False)


def test_erase_config_uses_only_declared_region(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(device_provision, "_run", lambda command, description: commands.append(tuple(command)))
    device_provision.erase_config(_bundle(tmp_path), "COM14", False)
    assert commands and commands[0][-3:] == ("erase_region", "0x9000", "0x6000")
    assert "erase_flash" not in commands[0]


def test_flash_validates_and_writes_exactly_five_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(device_provision, "_run", lambda command, description: commands.append(tuple(command)))
    device_provision.flash(_bundle(tmp_path), "COM14", False)
    command = commands[0]
    assert command.count("write_flash") == 1
    assert len([value for value in command if value.startswith("0x")]) == 5
    assert "0x9000" not in command
    assert "erase_flash" not in command


def test_flash_rejects_checksum_tampering(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bundle = _bundle(tmp_path)
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(bundle) as source, zipfile.ZipFile(tampered, "w") as destination:
        for name in source.namelist():
            payload = b"tampered" if name == "bootloader.bin" else source.read(name)
            destination.writestr(name, payload)
    result = device_provision.main(["flash", "--bundle", str(tampered), "--port", "COM14", "--dry-run"])
    assert result == 2
    assert "checksum mismatch" in capsys.readouterr().err


def test_cli_never_accepts_password_or_token_arguments(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "provision",
            "--bundle",
            str(_bundle(tmp_path)),
            "--password",
            "leaked",
            "--token",
            "leaked",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "unrecognized arguments" in result.stderr
