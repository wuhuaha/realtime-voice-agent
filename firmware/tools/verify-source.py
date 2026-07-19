from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml


class SourceVerificationError(RuntimeError):
    pass


def _run(command: list[str], *, cwd: Path | None = None) -> str:
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        raise SourceVerificationError(f"command failed: {command[0]}: {detail}")
    return result.stdout.strip()


def _load_source(repo_root: Path, source_id: str) -> dict[str, str]:
    lock_path = repo_root / "third_party/sources.lock.yaml"
    if not lock_path.is_file():
        raise SourceVerificationError(f"source lock is missing: {lock_path}")
    document: Any = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("sources"), list):
        raise SourceVerificationError("third_party source lock has no sources list")
    matches = [
        entry
        for entry in document["sources"]
        if isinstance(entry, dict) and entry.get("id") == source_id
    ]
    if len(matches) != 1:
        raise SourceVerificationError(f"source lock must contain exactly one {source_id!r} entry")
    entry = matches[0]
    if not isinstance(entry.get("revision"), str) or not isinstance(entry.get("version"), str):
        raise SourceVerificationError(f"source {source_id!r} requires revision and version strings")
    revision = entry["revision"].lower()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise SourceVerificationError(f"source {source_id!r} revision must be a full Git SHA")
    version = entry["version"].removeprefix("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SourceVerificationError(f"source {source_id!r} version must be exact major.minor.patch")
    return {"revision": revision, "version": version}


def verify_esp_idf(repo_root: Path, checkout: Path) -> None:
    source = _load_source(repo_root, "esp-idf")
    if not (checkout / ".git").exists() or not (checkout / "tools/idf.py").is_file():
        raise SourceVerificationError(f"ESP-IDF checkout is incomplete: {checkout}")
    tracked_changes = _run(
        ["git", "status", "--porcelain", "--untracked-files=no"], cwd=checkout
    )
    if tracked_changes:
        raise SourceVerificationError("refusing to use an ESP-IDF checkout with tracked changes")
    actual_revision = _run(["git", "rev-parse", "HEAD"], cwd=checkout).lower()
    if actual_revision != source["revision"]:
        raise SourceVerificationError(
            f"ESP-IDF revision mismatch: expected {source['revision']}, found {actual_revision}"
        )
    expected_tag = f"v{source['version']}"
    version_tags = _run(["git", "tag", "--points-at", "HEAD"], cwd=checkout).splitlines()
    if expected_tag not in version_tags:
        found = ", ".join(version_tags) if version_tags else "missing"
        raise SourceVerificationError(
            f"ESP-IDF version mismatch: expected tag {expected_tag!r}, found {found!r}"
        )
    print(f"Verified esp-idf {expected_tag} at {source['revision']}.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a toolchain checkout against third_party/sources.lock.yaml.")
    parser.add_argument("--source-id", required=True, choices=("esp-idf",))
    parser.add_argument("--checkout", required=True, type=Path)
    args = parser.parse_args()
    repo_root = Path(__file__).resolve().parents[2]
    try:
        if args.source_id == "esp-idf":
            verify_esp_idf(repo_root, args.checkout.resolve())
    except SourceVerificationError as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
