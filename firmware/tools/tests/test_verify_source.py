from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parents[1] / "verify-source.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("verify_source", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=repository, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _write_source_lock(repo_root: Path, revision: str, version: str) -> None:
    lock_path = repo_root / "third_party/sources.lock.yaml"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "sources": [
                    {
                        "id": "esp-idf",
                        "url": "https://example.invalid/esp-idf.git",
                        "revision": revision,
                        "version": version,
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_verify_esp_idf_revision_and_version(tmp_path: Path) -> None:
    module = _load_module()
    checkout = tmp_path / "esp-idf"
    tools = checkout / "tools"
    tools.mkdir(parents=True)
    (tools / "idf.py").write_text('print("ESP-IDF v5.5.2")\n', encoding="utf-8")
    _git(checkout, "init")
    _git(checkout, "config", "user.email", "firmware-test@example.invalid")
    _git(checkout, "config", "user.name", "Firmware Test")
    _git(checkout, "add", "tools/idf.py")
    _git(checkout, "commit", "-m", "fixture")
    revision = _git(checkout, "rev-parse", "HEAD")
    _git(checkout, "tag", "v5.5.2")
    repo_root = tmp_path / "delivery"

    _write_source_lock(repo_root, revision, "v5.5.2")
    module.verify_esp_idf(repo_root, checkout)

    (tools / "idf.py").write_text('print("ESP-IDF v5.5.2")\n# tracked change\n', encoding="utf-8")
    with pytest.raises(module.SourceVerificationError, match="tracked changes"):
        module.verify_esp_idf(repo_root, checkout)
    (tools / "idf.py").write_text('print("ESP-IDF v5.5.2")\n', encoding="utf-8")

    _write_source_lock(repo_root, revision, "v5.5.1")
    with pytest.raises(module.SourceVerificationError, match="version mismatch"):
        module.verify_esp_idf(repo_root, checkout)

    _write_source_lock(repo_root, "0" * 40, "v5.5.2")
    with pytest.raises(module.SourceVerificationError, match="revision mismatch"):
        module.verify_esp_idf(repo_root, checkout)
