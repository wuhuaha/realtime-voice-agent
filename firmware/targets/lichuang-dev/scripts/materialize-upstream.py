from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

SOURCE_ID = "xiaozhi-esp32"
LOCK_RELATIVE_PATH = Path("firmware/locks/xiaozhi-esp32.dependencies.lock")
CHECKOUT_RELATIVE_PATH = Path("external/xiaozhi-esp32")


class MaterializationError(RuntimeError):
    pass


def _run(command: list[str], *, cwd: Path | None = None) -> str:
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        raise MaterializationError(f"command failed: {command[0]}: {detail}")
    return result.stdout.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_source(lock_path: Path) -> dict[str, str]:
    document: Any = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("sources"), list):
        raise MaterializationError("third_party source lock has no sources list")
    matches = [entry for entry in document["sources"] if isinstance(entry, dict) and entry.get("id") == SOURCE_ID]
    if len(matches) != 1:
        raise MaterializationError(f"source lock must contain exactly one {SOURCE_ID!r} entry")
    entry = matches[0]
    required = ("url", "revision", "dependency_lock_sha256")
    if any(not isinstance(entry.get(field), str) or not entry[field] for field in required):
        raise MaterializationError(f"source {SOURCE_ID!r} is missing required string fields")
    revision = entry["revision"].lower()
    dependency_hash = entry["dependency_lock_sha256"].lower()
    if len(revision) != 40 or any(character not in "0123456789abcdef" for character in revision):
        raise MaterializationError("Xiaozhi revision must be a full 40-character Git SHA")
    if len(dependency_hash) != 64 or any(character not in "0123456789abcdef" for character in dependency_hash):
        raise MaterializationError("Xiaozhi dependency lock digest must be SHA256 hex")
    return {
        "url": entry["url"],
        "revision": revision,
        "dependency_lock_sha256": dependency_hash,
    }


def _normalize_remote(url: str) -> str:
    return url.strip().rstrip("/").removesuffix(".git").lower()


def _tracked_changes(checkout: Path) -> str:
    return _run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=checkout)


def _verify_origin(checkout: Path, expected_url: str) -> None:
    origin = _run(["git", "remote", "get-url", "origin"], cwd=checkout)
    if _normalize_remote(origin) != _normalize_remote(expected_url):
        raise MaterializationError("Xiaozhi checkout origin differs from third_party/sources.lock.yaml")


def _verify_checkout(checkout: Path, source: dict[str, str], controlled_lock: Path) -> None:
    if not (checkout / ".git").exists():
        raise MaterializationError(f"Xiaozhi checkout is not a Git worktree: {checkout}")
    if _tracked_changes(checkout):
        raise MaterializationError("refusing to use a Xiaozhi checkout with tracked changes")
    _verify_origin(checkout, source["url"])
    revision = _run(["git", "rev-parse", "HEAD"], cwd=checkout).lower()
    if revision != source["revision"]:
        raise MaterializationError(f"Xiaozhi revision mismatch: expected {source['revision']}, found {revision}")
    checkout_lock = checkout / "dependencies.lock"
    if not checkout_lock.is_file():
        raise MaterializationError("materialized Xiaozhi dependency lock is missing")
    if _sha256(checkout_lock) != _sha256(controlled_lock):
        raise MaterializationError("materialized Xiaozhi dependency lock differs from the controlled copy")
    if not (checkout / "LICENSE").is_file():
        raise MaterializationError("materialized Xiaozhi checkout has no LICENSE")


def materialize(repo_root: Path, *, verify_only: bool, verify_inputs_only: bool = False) -> None:
    source_lock = repo_root / "third_party/sources.lock.yaml"
    controlled_lock = repo_root / LOCK_RELATIVE_PATH
    checkout = repo_root / CHECKOUT_RELATIVE_PATH
    if not source_lock.is_file():
        raise MaterializationError(f"source lock is missing: {source_lock}")
    if not controlled_lock.is_file():
        raise MaterializationError(f"controlled dependency lock is missing: {controlled_lock}")
    source = _load_source(source_lock)
    controlled_hash = _sha256(controlled_lock)
    if controlled_hash != source["dependency_lock_sha256"]:
        raise MaterializationError("controlled dependency lock SHA256 differs from third_party/sources.lock.yaml")

    if verify_inputs_only:
        print(f"Verified {SOURCE_ID} source manifest and dependency lock {controlled_hash}.")
        return

    if verify_only:
        _verify_checkout(checkout, source, controlled_lock)
        print(f"Verified {SOURCE_ID} at {source['revision']} with dependency lock {controlled_hash}.")
        return

    created = not checkout.exists()
    if created:
        checkout.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "clone", "--filter=blob:none", "--no-checkout", source["url"], str(checkout)])
    elif not (checkout / ".git").exists():
        raise MaterializationError(f"refusing to replace non-Git path: {checkout}")

    if not created and _tracked_changes(checkout):
        raise MaterializationError("refusing to use a Xiaozhi checkout with tracked changes")
    if not created:
        _verify_origin(checkout, source["url"])

    current_revision = _run(["git", "rev-parse", "HEAD"], cwd=checkout).lower()
    if created:
        if current_revision != source["revision"]:
            _run(["git", "fetch", "--depth=1", "origin", source["revision"]], cwd=checkout)
        _run(["git", "checkout", "--detach", source["revision"]], cwd=checkout)
    elif current_revision != source["revision"]:
        _run(["git", "fetch", "--depth=1", "origin", source["revision"]], cwd=checkout)
        _run(["git", "checkout", "--detach", source["revision"]], cwd=checkout)

    checkout_lock = checkout / "dependencies.lock"
    if checkout_lock.exists() and _sha256(checkout_lock) != controlled_hash:
        raise MaterializationError("refusing to overwrite a differing checkout dependencies.lock")
    if not checkout_lock.exists():
        shutil.copyfile(controlled_lock, checkout_lock)
    _verify_checkout(checkout, source, controlled_lock)
    print(f"Materialized {SOURCE_ID} at {source['revision']} with dependency lock {controlled_hash}.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Materialize the pinned Xiaozhi firmware upstream.")
    verification = parser.add_mutually_exclusive_group()
    verification.add_argument("--verify-only", action="store_true", help="verify without cloning or changing files")
    verification.add_argument(
        "--verify-inputs-only",
        action="store_true",
        help="verify the source manifest and controlled dependency lock without requiring a checkout",
    )
    args = parser.parse_args()
    repo_root = Path(__file__).resolve().parents[4]
    try:
        materialize(
            repo_root,
            verify_only=args.verify_only,
            verify_inputs_only=args.verify_inputs_only,
        )
    except MaterializationError as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
