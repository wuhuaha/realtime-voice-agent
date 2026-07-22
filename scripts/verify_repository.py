from __future__ import annotations

import argparse
import codecs
import hashlib
import json
import re
import subprocess
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker

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
NATIVE_FIRMWARE_APP = "firmware/apps/voice_terminal"
NATIVE_COMPONENT_ROOT = "firmware/components"
LEGACY_FIRMWARE_PATH = "firmware/targets/lichuang-dev"
HISTORICAL_FIRMWARE_PATH = "firmware/reference/xiaozhi-overlay"
FIRMWARE_DEPENDENCY_LOCK = "firmware/locks/xiaozhi-esp32.dependencies.lock"
RESEARCH_REPOSITORY_NAMES = ("voice-agent-research", "realtime-voice-agent-research")
RESEARCH_BOUNDARY_SCAN_SUFFIXES = {
    ".bat",
    ".c",
    ".cc",
    ".cfg",
    ".cmake",
    ".cmd",
    ".conf",
    ".cpp",
    ".cxx",
    ".defaults",
    ".h",
    ".hh",
    ".hpp",
    ".hxx",
    ".ini",
    ".json",
    ".patch",
    ".properties",
    ".ps1",
    ".py",
    ".sh",
    ".toml",
    ".yaml",
    ".yml",
}
RESEARCH_BOUNDARY_SCAN_NAMES = {
    "cmakelists.txt",
    "dockerfile",
    "gnumakefile",
    "makefile",
    "procfile",
}
RESEARCH_BOUNDARY_EVIDENCE_EXEMPTIONS = {
    "migration/baseline/source-manifest.yaml",
}
FONT_THIRD_PARTY_NOTICE = "Copyright © 2014-2021 Adobe (http://www.adobe.com/)."
FONT_THIRD_PARTY_FILES = (
    "third_party/licenses/Noto-Sans-CJK-OFL-1.1.txt",
    "third_party/licenses/lv-font-conv-MIT.txt",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def repository_files(root: Path) -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    paths = tuple(item.decode("utf-8") for item in result.stdout.split(b"\0") if item)
    return tuple(path for path in paths if (root / path).is_file())


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
        return [f"missing manifest: {manifest_path.relative_to(root).as_posix()}"]
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
        if normalized_path.startswith(f"{LEGACY_FIRMWARE_PATH}/"):
            suffix = normalized_path.removeprefix(LEGACY_FIRMWARE_PATH)
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


def validate_firmware_source_lock(root: Path) -> list[str]:
    controlled_lock = root / FIRMWARE_DEPENDENCY_LOCK
    source_lock = root / "third_party" / "sources.lock.yaml"
    manifest = root / "migration" / "baseline" / "source-manifest.yaml"
    if not controlled_lock.is_file() or not source_lock.is_file() or not manifest.is_file():
        return ["firmware dependency lock identity inputs are incomplete"]

    actual = sha256(controlled_lock).lower()
    source_payload = yaml.safe_load(source_lock.read_text(encoding="utf-8"))
    source_entries = source_payload.get("sources", []) if isinstance(source_payload, dict) else []
    matches = [
        entry for entry in source_entries if isinstance(entry, dict) and entry.get("id") == "xiaozhi-esp32"
    ]
    errors: list[str] = []
    if len(matches) != 1 or matches[0].get("dependency_lock_sha256") != actual:
        errors.append("third_party source lock differs from controlled firmware dependency lock")

    manifest_payload = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    upstream = manifest_payload.get("upstream", {}) if isinstance(manifest_payload, dict) else {}
    if not isinstance(upstream, dict) or upstream.get("xiaozhi_dependencies_lock_sha256") != actual:
        errors.append("migration provenance differs from controlled firmware dependency lock")
    return errors


def validate_optional_legacy_rollback(root: Path) -> list[str]:
    """Validate pinned rollback provenance only while the legacy target is retained."""
    if not (root / LEGACY_FIRMWARE_PATH).exists():
        return []
    return [*validate_manifest(root), *validate_firmware_source_lock(root)]


def validate_firmware_composition(root: Path) -> list[str]:
    errors: list[str] = []
    production = root / NATIVE_FIRMWARE_APP
    legacy = root / HISTORICAL_FIRMWARE_PATH
    if legacy.exists():
        errors.append(f"legacy firmware runtime path still exists: {HISTORICAL_FIRMWARE_PATH}")
    for relative in (
        "README.md",
        "CMakeLists.txt",
        "partitions.csv",
        "sdkconfig.defaults",
        "main/CMakeLists.txt",
        "main/app_main.cc",
    ):
        if not (production / relative).is_file():
            errors.append(f"native firmware source missing: {NATIVE_FIRMWARE_APP}/{relative}")

    required_components = (
        "audio_frontend_esp_sr",
        "audio_pipeline",
        "board_lichuang_s3",
        "device_config",
        "native_runtime",
        "transport_udp",
        "transport_wss",
        "ui_lvgl",
        "voice_contracts",
        "voice_core",
        "voice_protocol",
    )
    for component in required_components:
        cmake = root / NATIVE_COMPONENT_ROOT / component / "CMakeLists.txt"
        if not cmake.is_file():
            errors.append(f"native firmware component missing: {NATIVE_COMPONENT_ROOT}/{component}/CMakeLists.txt")

    required_markers = {
        root / "docs" / "quality" / "release-readiness.md": (
            "状态：not ready",
            "Native clean build + size",
            "WSS voice loop",
            "UDP voice loop",
        ),
        root / "firmware" / "apps" / "voice_terminal" / "README.md": (
            "Product native ESP-IDF endpoint",
            LEGACY_FIRMWARE_PATH,
        ),
        root / ".github" / "workflows" / "ci.yml": (
            "native-firmware-host-contracts:",
            NATIVE_FIRMWARE_APP,
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


def validate_font_supply_chain(root: Path) -> list[str]:
    errors: list[str] = []
    notice_path = root / "firmware" / "components" / "ui_font_assets" / "THIRD_PARTY_NOTICES.md"
    if not notice_path.is_file():
        errors.append("font third-party notice is missing")
    elif FONT_THIRD_PARTY_NOTICE not in notice_path.read_text(encoding="utf-8"):
        errors.append("Noto Sans CJK copyright notice is missing")

    for relative in FONT_THIRD_PARTY_FILES:
        if not (root / relative).is_file():
            errors.append(f"font third-party license is missing: {relative}")
    return errors


def is_executable_or_config(path: Path) -> bool:
    lowered_name = path.name.lower()
    return (
        path.suffix.lower() in RESEARCH_BOUNDARY_SCAN_SUFFIXES
        or lowered_name in RESEARCH_BOUNDARY_SCAN_NAMES
        or lowered_name.startswith("dockerfile.")
        or (lowered_name.startswith(".env") and lowered_name.endswith(".example"))
    )


def read_boundary_text(path: Path) -> str:
    content = path.read_bytes()
    if content.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        raise ValueError("expected UTF-8 or BOM-marked UTF-16 text")
    if content.startswith(codecs.BOM_UTF8):
        encoding = "utf-8-sig"
    elif content.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encoding = "utf-16"
    else:
        if b"\0" in content:
            raise ValueError("expected UTF-8 or BOM-marked UTF-16 text")
        encoding = "utf-8"
    try:
        return content.decode(encoding)
    except UnicodeDecodeError as error:
        raise ValueError("expected UTF-8 or BOM-marked UTF-16 text") from error


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

    for raw_path in paths:
        normalized = raw_path.replace("\\", "/")
        path = root / raw_path
        if not path.is_file():
            continue
        if normalized in RESEARCH_BOUNDARY_EVIDENCE_EXEMPTIONS:
            continue
        if not is_executable_or_config(path):
            continue
        if normalized == "scripts/verify_repository.py":
            continue
        try:
            content = read_boundary_text(path).lower()
        except (OSError, ValueError) as error:
            errors.append(f"Product executable/config cannot be boundary-scanned: {normalized}: {error}")
            continue
        if any(repository_name in content for repository_name in RESEARCH_REPOSITORY_NAMES):
            errors.append(f"Product executable/config references Research repository: {normalized}")
    return errors


def validate_protocol(root: Path) -> list[str]:
    errors: list[str] = []
    registry = yaml.safe_load((root / "protocol" / "registry.yaml").read_text(encoding="utf-8"))
    control_ids = {protocol["id"] for protocol in registry["control_protocols"]}
    if registry.get("schema_version") != 2:
        errors.append("protocol registry must use schema_version 2")
    for section in ("control_protocols", "media_profiles"):
        for entry in registry.get(section, ()):
            for field in ("contract", "schema", "wire"):
                reference = entry.get(field)
                if isinstance(reference, str) and not (root / "protocol" / reference).is_file():
                    errors.append(
                        f"protocol registry reference missing: {entry.get('id')}:{field}: {reference}"
                    )
    if control_ids != {"xiaozhi-control-v1", "rva-control-v1"}:
        errors.append(f"unexpected control protocols: {sorted(control_ids)}")
    profile_ids = {profile["id"] for profile in registry["media_profiles"]}
    if profile_ids != {"wss-opus-v1", "wss-opus-v2", "udp-opus-gcm-v1"}:
        errors.append(f"unexpected media profiles: {sorted(profile_ids)}")
    for profile in registry["media_profiles"]:
        controls = set(profile.get("controls", ()))
        if not controls or not controls <= control_ids:
            errors.append(f"media profile has invalid controls: {profile.get('id')}")
    positive_path = root / "protocol" / "udp_opus_gcm_v1" / "fixtures" / "positive.json"
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

    rva_root = root / "protocol" / "rva_control_v1"
    schema = json.loads((rva_root / "messages.schema.json").read_text(encoding="utf-8"))
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as error:
        errors.append(f"invalid RVA control schema: {error}")
        return errors
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    rva_positive = json.loads((rva_root / "fixtures" / "positive.json").read_text(encoding="utf-8"))
    for vector in rva_positive["vectors"]:
        validation_errors = list(validator.iter_errors(vector["message"]))
        if validation_errors:
            errors.append(f"RVA positive fixture rejected: {vector['id']}")
    rva_negative = json.loads((rva_root / "fixtures" / "negative.json").read_text(encoding="utf-8"))
    for vector in rva_negative["schema_vectors"]:
        if not list(validator.iter_errors(vector["message"])):
            errors.append(f"RVA negative fixture accepted: {vector['id']}")
    contract = yaml.safe_load((rva_root / "contract.yaml").read_text(encoding="utf-8"))
    state_rules = {rule["id"] for rule in contract["state_rules"]}
    covered_rules = {vector["rule"] for vector in rva_negative["semantic_vectors"]}
    if covered_rules != state_rules:
        errors.append("RVA semantic fixtures do not cover every state rule")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.root.resolve()
    paths = repository_files(root)
    errors = [
        *validate_tracked_paths(paths),
        *validate_optional_legacy_rollback(root),
        *validate_protocol(root),
        *validate_firmware_composition(root),
        *validate_font_supply_chain(root),
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
