from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "generate_release_sbom", ROOT / "scripts" / "generate_release_sbom.py"
)
assert SPEC is not None and SPEC.loader is not None
SBOM = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SBOM)


def test_release_sbom_is_deterministic_and_covers_all_release_inputs() -> None:
    first = SBOM.serialize(SBOM.build_sbom(ROOT))
    second = SBOM.serialize(SBOM.build_sbom(ROOT))

    assert first == second
    document = json.loads(first)
    assert document["bomFormat"] == "CycloneDX"
    assert document["specVersion"] == "1.5"
    assert document["metadata"]["component"]["version"] == "0.1.0-alpha"
    assert len(document["metadata"]["properties"]) == len(SBOM.INPUTS)

    components = {item["bom-ref"]: item for item in document["components"]}
    assert any(ref.startswith("pypi:livekit-agents@") for ref in components)
    assert any(ref.startswith("pypi:websockets@") for ref in components)
    assert any(ref.startswith("idf-component:espressif/esp-sr@") for ref in components)
    assert any(ref.startswith("source:esp-idf@") for ref in components)
    assert not any(ref.startswith("pypi:pytest@") for ref in components)
    assert not any(ref.startswith("pypi:ruff@") for ref in components)

    livekit = next(item for item in document["components"] if item["name"] == "livekit-agents")
    assert "hashes" not in livekit
    assert any(
        item["name"] == "rva:lock-sdist-sha256" and len(item["value"]) == 64
        for item in livekit["properties"]
    )


def test_metadata_only_font_license_is_not_promoted_to_verified_license() -> None:
    document = SBOM.build_sbom(ROOT)
    font = next(item for item in document["components"] if item["name"] == "xiaozhi-fonts")
    noto = next(item for item in document["components"] if item["name"] == "noto-sans-cjk")
    esp_idf = next(item for item in document["components"] if item["name"] == "esp-idf")

    assert "licenses" not in font
    assert {item["name"]: item["value"] for item in font["properties"]}[
        "rva:license-evidence"
    ] == "declared metadata only; license file digest unavailable"
    assert noto["licenses"] == [{"license": {"id": "OFL-1.1"}}]

    notice_manifest = json.loads(
        (ROOT / "firmware/tools/public_bundle_notices.json").read_text(encoding="utf-8")
    )
    packaged_ofl = next(
        item
        for item in notice_manifest["notices"]
        if item["source_path"] == "third_party/licenses/Noto-Sans-CJK-OFL-1.1.txt"
    )
    assert packaged_ofl["sha256"] == SBOM.file_sha256(
        ROOT / "third_party/licenses/Noto-Sans-CJK-OFL-1.1.txt"
    )
    assert "licenses" not in esp_idf
    assert {item["name"]: item["value"] for item in esp_idf["properties"]}[
        "rva:license-evidence"
    ] == "declared metadata only; no verified license file"


def test_license_digest_mismatch_is_not_promoted(tmp_path: Path) -> None:
    license_path = tmp_path / "third_party" / "licenses" / "example.txt"
    license_path.parent.mkdir(parents=True)
    license_path.write_text("example license text\n", encoding="utf-8")
    lock_path = tmp_path / SBOM.SOURCES_LOCK
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        """schema_version: 1
sources:
  - id: example
    url: https://example.invalid/source
    version: 1.0.0
    license: MIT
    license_file: third_party/licenses/example.txt
    license_file_sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    purpose: digest mismatch test
""",
        encoding="utf-8",
    )

    component = SBOM.source_components(tmp_path)[0]
    assert "licenses" not in component
    assert {item["name"]: item["value"] for item in component["properties"]}[
        "rva:license-evidence"
    ] == "declared metadata only; license file digest mismatch"


def test_check_mode_detects_stale_output(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "release-sbom.cdx.json"
    monkeypatch.setattr("sys.argv", ["generate_release_sbom.py", "--output", str(output)])
    assert SBOM.main() == 0

    monkeypatch.setattr(
        "sys.argv", ["generate_release_sbom.py", "--output", str(output), "--check"]
    )
    assert SBOM.main() == 0
    output.write_text("{}\n", encoding="utf-8")
    assert SBOM.main() == 1
