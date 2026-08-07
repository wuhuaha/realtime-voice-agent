from __future__ import annotations

import argparse
import hashlib
import json
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

ROOT = Path(__file__).resolve().parents[1]
SERVER_LOCK = Path("server/uv.lock")
DESKTOP_LOCK = Path("clients/desktop_reference/uv.lock")
FIRMWARE_LOCK = Path("firmware/apps/voice_terminal/dependencies.lock")
SOURCES_LOCK = Path("third_party/sources.lock.yaml")
INPUTS = (SERVER_LOCK, DESKTOP_LOCK, FIRMWARE_LOCK, SOURCES_LOCK)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def component_ref(ecosystem: str, name: str, version: str) -> str:
    return f"{ecosystem}:{name}@{version}"


def pypi_purl(name: str, version: str) -> str:
    return f"pkg:pypi/{quote(name, safe='._-')}@{quote(version, safe='._-+')}"


def registry_purl(name: str, version: str) -> str:
    return f"pkg:generic/{quote(name, safe='/._-')}@{quote(version, safe='._-+~')}"


def add_scope(component: dict[str, Any], scope: str) -> None:
    properties = component.setdefault("properties", [])
    item = {"name": "rva:release-scope", "value": scope}
    if item not in properties:
        properties.append(item)


def add_property(component: dict[str, Any], name: str, value: str) -> None:
    item = {"name": name, "value": value}
    properties = component.setdefault("properties", [])
    if item not in properties:
        properties.append(item)


def add_hash(component: dict[str, Any], digest: str | None) -> None:
    if not digest:
        return
    normalized = digest.removeprefix("sha256:")
    if len(normalized) == 64:
        component["hashes"] = [{"alg": "SHA-256", "content": normalized}]


def python_components(
    root: Path,
    lock_path: Path,
    scope: str,
    release_roots: dict[str, tuple[str, ...]],
) -> list[dict[str, Any]]:
    lock = tomllib.loads((root / lock_path).read_text(encoding="utf-8"))
    packages = {str(package["name"]): package for package in lock.get("package", [])}
    selected: set[str] = set()
    queue = list(release_roots.items())
    while queue:
        name, requested_extras = queue.pop()
        package = packages.get(name)
        if package is None:
            raise ValueError(f"{lock_path}: dependency {name!r} is absent from lock")
        first_visit = name not in selected
        selected.add(name)
        dependencies = list(package.get("dependencies", [])) if first_visit else []
        optional = package.get("optional-dependencies", {})
        for extra in requested_extras:
            dependencies.extend(optional.get(extra, []))
        for dependency in dependencies:
            extras = dependency.get("extra") or dependency.get("extras") or ()
            queue.append((str(dependency["name"]), tuple(extras)))

    components: list[dict[str, Any]] = []
    for name in sorted(selected):
        package = packages[name]
        source = package.get("source", {})
        if "registry" not in source:
            continue
        version = str(package["version"])
        component: dict[str, Any] = {
            "type": "library",
            "bom-ref": component_ref("pypi", name, version),
            "name": name,
            "version": version,
            "purl": pypi_purl(name, version),
        }
        sdist = package.get("sdist") or {}
        add_scope(component, scope)
        sdist_digest = str(sdist.get("hash") or "").removeprefix("sha256:")
        if len(sdist_digest) == 64:
            add_property(component, "rva:lock-sdist-sha256", sdist_digest)
        components.append(component)
    return components


def firmware_components(root: Path) -> list[dict[str, Any]]:
    lock = yaml.safe_load((root / FIRMWARE_LOCK).read_text(encoding="utf-8"))
    components: list[dict[str, Any]] = []
    for name, package in lock.get("dependencies", {}).items():
        if name == "idf":
            continue
        version = str(package["version"])
        component: dict[str, Any] = {
            "type": "library",
            "bom-ref": component_ref("idf-component", name, version),
            "name": name,
            "version": version,
            "purl": registry_purl(name, version),
        }
        add_hash(component, package.get("component_hash"))
        add_scope(component, "esp32-firmware")
        components.append(component)
    return components


def source_components(root: Path) -> list[dict[str, Any]]:
    lock = yaml.safe_load((root / SOURCES_LOCK).read_text(encoding="utf-8"))
    components: list[dict[str, Any]] = []
    for source in lock.get("sources", []):
        name = str(source["id"])
        version = str(source.get("version") or source.get("revision") or "unknown")
        component: dict[str, Any] = {
            "type": "framework" if name == "esp-idf" else "file",
            "bom-ref": component_ref("source", name, version),
            "name": name,
            "version": version,
            "externalReferences": [{"type": "vcs", "url": str(source["url"])}],
            "properties": [
                {"name": "rva:release-scope", "value": "esp32-firmware"},
                {"name": "rva:purpose", "value": str(source["purpose"])},
            ],
        }
        revision = source.get("revision")
        if revision:
            component["properties"].append({"name": "rva:revision", "value": str(revision)})
        add_hash(component, source.get("artifact_sha256"))

        declared_license = source.get("license")
        license_file = source.get("license_file")
        expected_license_digest = source.get("license_file_sha256")
        license_is_verified = False
        license_evidence = "declared metadata only; no verified license file"
        if license_file and expected_license_digest:
            actual_license_digest = file_sha256(root / str(license_file))
            if actual_license_digest == str(expected_license_digest):
                license_is_verified = True
                license_evidence = "license file digest verified"
                component["properties"].append(
                    {"name": "rva:license-file-sha256", "value": actual_license_digest}
                )
            else:
                license_evidence = "declared metadata only; license file digest mismatch"
        elif license_file:
            license_evidence = "declared metadata only; license file digest unavailable"
        if declared_license and license_is_verified:
            component["licenses"] = [{"license": {"id": str(declared_license)}}]
        elif declared_license:
            component["properties"].append(
                {"name": "rva:upstream-license-metadata", "value": str(declared_license)}
            )
            component["properties"].append(
                {"name": "rva:license-evidence", "value": license_evidence}
            )
        components.append(component)
    return components


def merge_components(components: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for component in components:
        reference = component["bom-ref"]
        current = merged.get(reference)
        if current is None:
            merged[reference] = component
            continue
        for prop in component.get("properties", []):
            if prop not in current.setdefault("properties", []):
                current["properties"].append(prop)
    for component in merged.values():
        component.get("properties", []).sort(key=lambda item: (item["name"], item["value"]))
    return sorted(merged.values(), key=lambda item: item["bom-ref"])


def build_sbom(root: Path = ROOT) -> dict[str, Any]:
    missing = [str(path) for path in INPUTS if not (root / path).is_file()]
    if missing:
        raise FileNotFoundError(f"missing release SBOM input(s): {', '.join(missing)}")

    components = merge_components(
        python_components(
            root,
            SERVER_LOCK,
            "server",
            {"realtime-worker": (), "session-director": ()},
        )
        + python_components(
            root,
            DESKTOP_LOCK,
            "desktop-reference",
            {"rva-desktop": ("interactive",)},
        )
        + firmware_components(root)
        + source_components(root)
    )
    input_properties = [
        {"name": f"rva:input-sha256:{path.as_posix()}", "value": file_sha256(root / path)}
        for path in INPUTS
    ]
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "bom-ref": "application:realtime-voice-agent@0.1.0-alpha.1",
                "name": "realtime-voice-agent",
                "version": "0.1.0-alpha.1",
            },
            "properties": input_properties,
        },
        "components": components,
    }


def serialize(sbom: dict[str, Any]) -> str:
    return json.dumps(sbom, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the deterministic v0.1.0-alpha.1 release SBOM")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check", action="store_true", help="fail if output differs from current lock inputs")
    args = parser.parse_args()

    sbom = build_sbom()
    rendered = serialize(sbom)
    if args.check:
        if not args.output.is_file() or args.output.read_text(encoding="utf-8") != rendered:
            print(f"release SBOM is missing or stale: {args.output}")
            return 1
        print(f"release SBOM is current: {args.output}")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"generated {args.output} ({len(sbom['components'])} components)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
