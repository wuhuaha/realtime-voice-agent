from __future__ import annotations

import argparse
import hashlib
import json
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
    errors: list[str] = []
    for entry in payload.get("files", []):
        relative = Path(entry["path"])
        target = (root / relative).resolve()
        if not target.is_relative_to(root.resolve()):
            errors.append(f"manifest path escapes repository: {relative}")
            continue
        if not target.is_file():
            errors.append(f"manifest file missing: {relative.as_posix()}")
            continue
        actual = sha256(target)
        if actual.lower() != str(entry["sha256"]).lower():
            errors.append(f"manifest hash mismatch: {relative.as_posix()}")
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
    errors = [
        *validate_tracked_paths(tracked_files(root)),
        *validate_manifest(root),
        *validate_protocol(root),
    ]
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("Repository contracts are valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
