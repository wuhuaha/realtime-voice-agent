from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

import yaml

FORBIDDEN_PARTS = {
    ".env",
    ".env.local",
    "direct_webrtc_v1",
    "direct_session_v1",
    "voice_agent_server",
    "client/archive",
}
FORBIDDEN_SUFFIXES = {".bin", ".elf", ".map", ".wav", ".pcm", ".key", ".pem"}
SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
PRODUCTION_FIRMWARE_PATH = "firmware/targets/lichuang-dev"
HISTORICAL_FIRMWARE_PATH = "firmware/reference/xiaozhi-overlay"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tracked_files(root: Path) -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return tuple(item.decode("utf-8") for item in result.stdout.split(b"\0") if item)


def validate_tracked_paths(paths: tuple[str, ...]) -> list[str]:
    errors: list[str] = []
    for raw_path in paths:
        normalized = raw_path.replace("\\", "/")
        lowered = normalized.lower()
        if lowered.endswith((".env.example", ".env.local.example")):
            continue
        if any(part in lowered for part in FORBIDDEN_PARTS):
            errors.append(f"forbidden tracked path: {raw_path}")
        if Path(normalized).suffix.lower() in FORBIDDEN_SUFFIXES:
            errors.append(f"forbidden tracked artifact: {raw_path}")
    return errors


def validate_manifest(root: Path) -> list[str]:
    manifest_path = root / "migration" / "baseline" / "source-manifest.yaml"
    if not manifest_path.is_file():
        return [f"missing manifest: {manifest_path.relative_to(root)}"]
    payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return ["source manifest must be a mapping"]
    entries = payload.get("files")
    if not isinstance(entries, list):
        return ["source manifest files must be a list"]
    errors: list[str] = []
    manifest_paths: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("production_path"), str):
            errors.append("manifest file entry must contain a string production_path")
            continue
        normalized_path = entry["production_path"].replace("\\", "/")
        if normalized_path in manifest_paths:
            errors.append(f"duplicate manifest production_path: {normalized_path}")
            continue
        manifest_paths.add(normalized_path)
        source_path = entry.get("source_path")
        if source_path is not None and not isinstance(source_path, str):
            errors.append(f"invalid manifest source_path: {normalized_path}")
        if normalized_path.startswith(f"{PRODUCTION_FIRMWARE_PATH}/"):
            suffix = normalized_path.removeprefix(PRODUCTION_FIRMWARE_PATH)
            expected_source = f"{HISTORICAL_FIRMWARE_PATH}{suffix}"
            if source_path != expected_source:
                errors.append(f"firmware provenance path mismatch: {normalized_path}")
        expected_hash = entry.get("sha256")
        if not isinstance(expected_hash, str) or SHA256_PATTERN.fullmatch(expected_hash) is None:
            errors.append(f"invalid manifest SHA256: {normalized_path}")
            continue
        relative = Path(normalized_path)
        target = (root / relative).resolve()
        if not target.is_relative_to(root.resolve()):
            errors.append(f"manifest path escapes repository: {relative}")
            continue
        if not target.is_file():
            errors.append(f"manifest file missing: {relative.as_posix()}")
            continue
        actual = sha256(target)
        if actual.lower() != expected_hash.lower():
            errors.append(f"manifest hash mismatch: {relative.as_posix()}")
    traceability = payload.get("traceability", {})
    if not isinstance(traceability, dict):
        errors.append("source manifest traceability must be a mapping")
    else:
        for capability, paths in traceability.items():
            if not isinstance(paths, list) or not paths:
                errors.append(f"traceability group must be a non-empty list: {capability}")
                continue
            for raw_path in paths:
                if not isinstance(raw_path, str) or raw_path.replace("\\", "/") not in manifest_paths:
                    errors.append(f"traceability path is not hashed in manifest: {capability}: {raw_path}")
    return errors


def validate_firmware_composition(root: Path) -> list[str]:
    errors: list[str] = []
    production = root / PRODUCTION_FIRMWARE_PATH
    legacy = root / HISTORICAL_FIRMWARE_PATH
    if legacy.exists():
        errors.append(f"legacy firmware runtime path still exists: {HISTORICAL_FIRMWARE_PATH}")
    for relative in (
        "README.md",
        "sdkconfig.defaults",
        "scripts/materialize-upstream.ps1",
        "scripts/verify-source-contract.ps1",
        "scripts/build.ps1",
    ):
        if not (production / relative).is_file():
            errors.append(f"production firmware source missing: {PRODUCTION_FIRMWARE_PATH}/{relative}")

    required_markers = {
        root / "firmware" / "MIGRATION_STATUS.md": (
            "唯一 production firmware composition",
            "non-release component-extraction prototype",
            "全量 clean build 完成 `2215/2215`",
        ),
        root / "firmware" / "device" / "README.md": (
            "non-release component-extraction prototype",
            PRODUCTION_FIRMWARE_PATH,
        ),
        root / ".github" / "workflows" / "ci.yml": (
            "production-source-contract:",
            f"./{PRODUCTION_FIRMWARE_PATH}/scripts/materialize-upstream.ps1",
        ),
    }
    for path, markers in required_markers.items():
        if not path.is_file():
            errors.append(f"firmware composition document missing: {path.relative_to(root).as_posix()}")
            continue
        content = path.read_text(encoding="utf-8")
        for marker in markers:
            if marker not in content:
                errors.append(
                    f"firmware composition marker missing: {path.relative_to(root).as_posix()}: {marker}"
                )
    return errors


def validate_research_boundary(root: Path, paths: tuple[str, ...]) -> list[str]:
    errors: list[str] = []
    decision = root / "docs" / "decisions" / "0004-research-production-boundary.md"
    required_markers = (
        "唯一 authoring source",
        "不得读取 `voice-agent-research`",
        "manifest不是运行配置",
    )
    if not decision.is_file():
        errors.append("missing Product/Research boundary decision")
    else:
        content = decision.read_text(encoding="utf-8")
        for marker in required_markers:
            if marker not in content:
                errors.append(f"Product/Research boundary marker missing: {marker}")

    executable_suffixes = {".cmake", ".ps1", ".py", ".toml"}
    for raw_path in paths:
        normalized = raw_path.replace("\\", "/")
        path = root / raw_path
        if not path.is_file():
            continue
        if path.suffix.lower() not in executable_suffixes and path.name != "CMakeLists.txt":
            continue
        if normalized == "scripts/verify_repository.py":
            continue
        try:
            content = path.read_text(encoding="utf-8").lower()
        except UnicodeDecodeError:
            continue
        if "voice-agent-research" in content or "realtime-voice-agent-research" in content:
            errors.append(f"Product executable/config references Research repository: {normalized}")
    return errors


def validate_protocol(root: Path) -> list[str]:
    errors: list[str] = []
    registry = yaml.safe_load((root / "protocol" / "registry.yaml").read_text(encoding="utf-8"))
    profile_ids = {profile["id"] for profile in registry["media_profiles"]}
    if profile_ids != {"wss-opus-v1", "udp-opus-gcm-v1"}:
        errors.append(f"unexpected media profiles: {sorted(profile_ids)}")
    positive_path = root / "protocol" / "xiaozhi_udp_v1" / "fixtures" / "positive.json"
    positive = json.loads(positive_path.read_text(encoding="utf-8"))
    for vector in positive["vectors"]:
        datagram = bytes.fromhex(vector["datagram_hex"])
        header = bytes.fromhex(vector["header_hex"])
        encrypted = bytes.fromhex(vector["ciphertext_and_tag_hex"])
        if datagram != header + encrypted:
            errors.append(f"UDP fixture byte mismatch: {vector['id']}")
        if len(header) != positive["header_bytes"]:
            errors.append(f"UDP fixture header length mismatch: {vector['id']}")
        if len(datagram) > positive["max_datagram_bytes"]:
            errors.append(f"UDP fixture exceeds MTU: {vector['id']}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.root.resolve()
    paths = tracked_files(root)
    errors = [
        *validate_tracked_paths(paths),
        *validate_manifest(root),
        *validate_protocol(root),
        *validate_firmware_composition(root),
        *validate_research_boundary(root, paths),
    ]
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("Repository contracts are valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
