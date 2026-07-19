from __future__ import annotations

import hashlib
import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/materialize-upstream.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("materialize_upstream", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=repository, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def test_materialize_and_verify_pinned_checkout(tmp_path: Path) -> None:
    module = _load_module()
    source_repository = tmp_path / "source"
    source_repository.mkdir()
    _git(source_repository, "init")
    _git(source_repository, "config", "user.email", "firmware-test@example.invalid")
    _git(source_repository, "config", "user.name", "Firmware Test")
    (source_repository / "LICENSE").write_text("MIT License\n", encoding="utf-8")
    (source_repository / ".gitignore").write_text("dependencies.lock\n", encoding="utf-8")
    _git(source_repository, "add", "LICENSE", ".gitignore")
    _git(source_repository, "commit", "-m", "fixture")
    revision = _git(source_repository, "rev-parse", "HEAD")

    repository_root = tmp_path / "delivery"
    source_lock = repository_root / "third_party/sources.lock.yaml"
    controlled_lock = repository_root / "firmware/locks/xiaozhi-esp32.dependencies.lock"
    source_lock.parent.mkdir(parents=True)
    controlled_lock.parent.mkdir(parents=True)
    lock_bytes = b"dependencies:\n  fixture: 1\n"
    controlled_lock.write_bytes(lock_bytes)
    lock_hash = hashlib.sha256(lock_bytes).hexdigest()
    source_lock.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "sources": [
                    {
                        "id": "xiaozhi-esp32",
                        "url": str(source_repository),
                        "revision": revision,
                        "dependency_lock_sha256": lock_hash,
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    module.materialize(repository_root, verify_only=False, verify_inputs_only=True)
    assert not (repository_root / "external").exists()
    module.materialize(repository_root, verify_only=False)
    module.materialize(repository_root, verify_only=True)
    checkout = repository_root / "external/xiaozhi-esp32"
    assert _git(checkout, "rev-parse", "HEAD") == revision
    assert (checkout / "LICENSE").read_text(encoding="utf-8") == "MIT License\n"
    assert (checkout / "dependencies.lock").read_bytes() == lock_bytes
    assert _git(checkout, "status", "--porcelain") == ""

    (checkout / "LICENSE").write_text("tracked change\n", encoding="utf-8")
    with pytest.raises(module.MaterializationError, match="tracked changes"):
        module.materialize(repository_root, verify_only=False)
    with pytest.raises(module.MaterializationError, match="tracked changes"):
        module.materialize(repository_root, verify_only=True)
    (checkout / "LICENSE").write_text("MIT License\n", encoding="utf-8")

    (checkout / "dependencies.lock").write_bytes(b"tampered\n")
    with pytest.raises(module.MaterializationError, match="differs from the controlled copy"):
        module.materialize(repository_root, verify_only=True)


def test_wrong_origin_fails_before_revision_fetch_or_checkout(tmp_path: Path) -> None:
    module = _load_module()
    source_repository = tmp_path / "source"
    source_repository.mkdir()
    _git(source_repository, "init")
    _git(source_repository, "config", "user.email", "firmware-test@example.invalid")
    _git(source_repository, "config", "user.name", "Firmware Test")
    (source_repository / "LICENSE").write_text("MIT License\n", encoding="utf-8")
    (source_repository / ".gitignore").write_text("dependencies.lock\n", encoding="utf-8")
    _git(source_repository, "add", "LICENSE", ".gitignore")
    _git(source_repository, "commit", "-m", "pinned fixture")
    pinned_revision = _git(source_repository, "rev-parse", "HEAD")
    (source_repository / "README.md").write_text("later revision\n", encoding="utf-8")
    _git(source_repository, "add", "README.md")
    _git(source_repository, "commit", "-m", "later fixture")
    later_revision = _git(source_repository, "rev-parse", "HEAD")

    repository_root = tmp_path / "delivery"
    source_lock = repository_root / "third_party/sources.lock.yaml"
    controlled_lock = repository_root / "firmware/locks/xiaozhi-esp32.dependencies.lock"
    source_lock.parent.mkdir(parents=True)
    controlled_lock.parent.mkdir(parents=True)
    lock_bytes = b"dependencies:\n  fixture: 1\n"
    controlled_lock.write_bytes(lock_bytes)
    source_lock.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "sources": [
                    {
                        "id": "xiaozhi-esp32",
                        "url": str(source_repository),
                        "revision": pinned_revision,
                        "dependency_lock_sha256": hashlib.sha256(lock_bytes).hexdigest(),
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    module.materialize(repository_root, verify_only=False)
    checkout = repository_root / "external/xiaozhi-esp32"
    _git(checkout, "checkout", "--detach", later_revision)
    _git(checkout, "remote", "set-url", "origin", str(tmp_path / "wrong-origin"))

    with pytest.raises(module.MaterializationError, match="origin differs"):
        module.materialize(repository_root, verify_only=False)
    assert _git(checkout, "rev-parse", "HEAD") == later_revision
