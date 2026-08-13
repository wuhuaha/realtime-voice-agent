from __future__ import annotations

import argparse
import codecs
import json
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
FORBIDDEN_PREFIXES = (
    "firmware/targets/lichuang-dev/",
    "protocol/rva_control_v2/",
    "protocol/udp_opus_gcm_v2/",
    "protocol/xiaozhi_control_v1/",
    "server/apps/realtime_worker/src/realtime_worker/bindings/xiaozhi/",
)
FORBIDDEN_FILES = {
    "server/apps/realtime_worker/src/realtime_worker/bindings/xiaozhi_runtime.py",
    "server/apps/realtime_worker/src/realtime_worker/bindings/xiaozhi_udp.py",
}
NATIVE_FIRMWARE_APP = "firmware/apps/voice_terminal"
NATIVE_COMPONENT_ROOT = "firmware/components"
HISTORICAL_FIRMWARE_PATH = "firmware/reference/xiaozhi-overlay"
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
FONT_THIRD_PARTY_NOTICE = "Copyright © 2014-2021 Adobe (http://www.adobe.com/)."
FONT_THIRD_PARTY_FILES = (
    "third_party/licenses/Noto-Sans-CJK-OFL-1.1.txt",
    "third_party/licenses/xiaozhi-fonts-MIT.txt",
)
PUBLIC_RESEARCH_GOVERNANCE_MARKERS = {
    ".github/ISSUE_TEMPLATE/feature.yml": (
        "Product 是所有公开建议的统一入口",
        "可公开、可复现且已脱敏",
    ),
    ".github/ISSUE_TEMPLATE/integration.yml": (
        "Product 是所有公开集成建议的统一入口",
        "可公开、可复现且已脱敏",
    ),
    "AGENTS.md": (
        "不得要求外部贡献者访问",
        "来源与许可证",
    ),
    "CONTRIBUTING.md": (
        "来源与许可证明确",
        "不得引用私有仓库",
    ),
    "docs/decisions/0004-research-production-boundary.md": (
        "来源/许可证",
        "不得以访问私有 Research 为成立前提",
    ),
    "docs/quality/server-capacity.md": (
        "不得用私有 artifact",
    ),
}
PUBLIC_CAPACITY_STATUS_MARKERS = {
    "docs/quality/server-capacity.md": "状态：`not_run / no public reproducible baseline`",
    "docs/quality/release-readiness.md": "没有从公开可恢复 commit 运行并带有公开脱敏证据的容量基线",
    "docs/quality/known-limitations.md": "不作为 `measured` 结果、资源建议或",
    "server/README.md": "公开可复现的容量基线、资源建议或容量 SLO",
}
LEGACY_PUBLIC_CAPACITY_FINGERPRINTS = (
    "f53997b137c5fcbd97568c516e6c1a658679a78c",
    "rva-capacity-exact:f53997b-b636abf",
    "sha256:ca94ef44fd5c35c18fc9589e4f6fbcea0bd4e962dab629a254653ba06ee5f89d",
    "c971e047cabc04dd57941491ecdb3c9ec7641987635b4a7fbd7063a04fb5543c",
    "/srv/voice/benchmarks/rva-capacity/",
    "状态：`measured / bounded capacity baseline`",
    "WSS `wss-opus/1` 和 UDP `udp-opus-gcm/1` 均已验证 10、100 个活跃 session",
    "100 并发在单 Worker、`2 vCPU / 1 GiB` 下通过 5 分钟 steady 和 5 分钟 short-session churn",
    "`2 vCPU / 2 GiB` 下的 130 并发 10 分钟余量验证也通过",
    "provider-free 实验最低配置 | 推荐配置 | Worker 数 | `max_sessions` | `nofile` | 带宽建议",
    "`pids_limit`，10 并发实验最低建议不低于 `4,500`",
    "有限容量阶梯已完成：WSS 单 Worker 为 200 通过、300 未通过",
    "`tools/capacity_sharded.py`、`tools/capacity_cluster.py` 及 Research 中的实验编排",
)


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
        if lowered.startswith(FORBIDDEN_PREFIXES) or lowered in FORBIDDEN_FILES:
            errors.append(f"retired runtime path is tracked: {raw_path}")
        if lowered.endswith((".env.example", ".env.local.example")):
            continue
        if any(part in lowered for part in FORBIDDEN_PARTS):
            errors.append(f"forbidden tracked path: {raw_path}")
        if Path(normalized).suffix.lower() in FORBIDDEN_SUFFIXES:
            errors.append(f"forbidden tracked artifact: {raw_path}")
    return errors


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
            "状态：",
            "Native clean build + size",
            "WSS voice loop",
            "UDP voice loop",
        ),
        root / "firmware" / "apps" / "voice_terminal" / "README.md": (
            "Product native ESP-IDF endpoint",
            "rva/1",
            "wss-opus/1",
            "udp-opus-gcm/1",
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


def validate_public_research_governance(
    root: Path, paths: tuple[str, ...] | None = None
) -> list[str]:
    errors: list[str] = []
    for relative, markers in PUBLIC_RESEARCH_GOVERNANCE_MARKERS.items():
        path = root / relative
        if not path.is_file():
            errors.append(f"public Research governance file missing: {relative}")
            continue
        content = path.read_text(encoding="utf-8")
        for marker in markers:
            if marker not in content:
                errors.append(f"public Research governance marker missing: {relative}: {marker}")
    for relative, marker in PUBLIC_CAPACITY_STATUS_MARKERS.items():
        path = root / relative
        if not path.is_file():
            errors.append(f"public capacity governance file missing: {relative}")
            continue
        content = " ".join(path.read_text(encoding="utf-8").split())
        if marker not in content:
            errors.append(f"public capacity status marker missing: {relative}: {marker}")

    scan_paths = paths
    if scan_paths is None:
        scan_paths = tuple(
            sorted({*PUBLIC_RESEARCH_GOVERNANCE_MARKERS, *PUBLIC_CAPACITY_STATUS_MARKERS})
        )
    for relative in scan_paths:
        normalized = relative.replace("\\", "/")
        if not normalized.lower().endswith(".md"):
            continue
        path = root / relative
        if not path.is_file():
            continue
        content = " ".join(path.read_text(encoding="utf-8").split())
        for fingerprint in LEGACY_PUBLIC_CAPACITY_FINGERPRINTS:
            if fingerprint in content:
                errors.append(
                    f"legacy public capacity evidence is tracked: {normalized}: {fingerprint}"
                )
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
    if control_ids != {"rva/1"}:
        errors.append(f"unexpected control protocols: {sorted(control_ids)}")
    profile_ids = {profile["id"] for profile in registry["media_profiles"]}
    if profile_ids != {"wss-opus/1", "udp-opus-gcm/1"}:
        errors.append(f"unexpected media profiles: {sorted(profile_ids)}")
    for profile in registry["media_profiles"]:
        controls = set(profile.get("controls", ()))
        if not controls or not controls <= control_ids:
            errors.append(f"media profile has invalid controls: {profile.get('id')}")
        if profile.get("media_wire_version") != 1:
            errors.append(f"media profile must use wire version 1: {profile.get('id')}")
        if profile.get("uplink_generation") != 0:
            errors.append(f"media profile must use uplink generation zero: {profile.get('id')}")
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

    rva_root = root / "protocol" / "rva_v1"
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
        *validate_protocol(root),
        *validate_firmware_composition(root),
        *validate_font_supply_chain(root),
        *validate_research_boundary(root, paths),
        *validate_public_research_governance(root, paths),
    ]
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("Repository contracts are valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
