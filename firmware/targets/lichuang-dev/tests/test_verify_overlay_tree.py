from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/verify-overlay-tree.py"
BUILD_SCRIPT = Path(__file__).resolve().parents[1] / "scripts/build.ps1"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("verify_overlay_tree", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(["git", *arguments], cwd=repository, capture_output=True, text=True, check=True)
    return result.stdout


def _git_bytes(repository: Path, *arguments: str) -> bytes:
    result = subprocess.run(["git", *arguments], cwd=repository, capture_output=True, check=True)
    return result.stdout


def _canonical_fixture(tmp_path: Path) -> tuple[Path, Path]:
    checkout = tmp_path / "checkout"
    integration = tmp_path / "integration"
    checkout.mkdir()
    (checkout / "main").mkdir()
    (checkout / "main/base.cc").write_text("baseline\n", encoding="utf-8")
    (checkout / "main/unrelated.cc").write_text("unchanged\n", encoding="utf-8")
    _git(checkout, "init")
    _git(checkout, "config", "user.email", "firmware-test@example.invalid")
    _git(checkout, "config", "user.name", "Firmware Test")
    _git(checkout, "add", "main")
    _git(checkout, "commit", "-m", "fixture")

    patch_directory = integration / "overlay"
    overlay_files = integration / "overlay-files/main"
    patch_directory.mkdir(parents=True)
    overlay_files.mkdir(parents=True)
    (checkout / "main/base.cc").write_text("canonical patch\n", encoding="utf-8")
    (patch_directory / "0001-canonical.patch").write_bytes(_git_bytes(checkout, "diff", "--binary"))
    (checkout / "main/base.cc").write_text("baseline\n", encoding="utf-8")
    (overlay_files / "added.cc").write_text("canonical overlay file\n", encoding="utf-8")

    _git(checkout, "apply", str(patch_directory / "0001-canonical.patch"))
    shutil.copyfile(overlay_files / "added.cc", checkout / "main/added.cc")
    (checkout / "main/voice_agent_local_config.h").write_text("generated locally\n", encoding="utf-8")
    return checkout, integration


def test_accepts_exact_canonical_overlay_tree(tmp_path: Path) -> None:
    module = _load_module()
    checkout, integration = _canonical_fixture(tmp_path)

    module.verify_overlay_tree(checkout, integration)


def test_rejects_tracked_main_pollution(tmp_path: Path) -> None:
    module = _load_module()
    checkout, integration = _canonical_fixture(tmp_path)
    (checkout / "main/unrelated.cc").write_text("local pollution\n", encoding="utf-8")

    with pytest.raises(module.OverlayTreeError, match="outside canonical overlay"):
        module.verify_overlay_tree(checkout, integration)


def test_rejects_untracked_main_pollution(tmp_path: Path) -> None:
    module = _load_module()
    checkout, integration = _canonical_fixture(tmp_path)
    (checkout / "main/rogue.cc").write_text("local pollution\n", encoding="utf-8")

    with pytest.raises(module.OverlayTreeError, match="unexpected untracked main source"):
        module.verify_overlay_tree(checkout, integration)


def test_rejects_ignored_untracked_main_pollution(tmp_path: Path) -> None:
    module = _load_module()
    checkout, integration = _canonical_fixture(tmp_path)
    (checkout / "main/.gitignore").write_text("ignored-rogue.cc\n", encoding="utf-8")
    _git(checkout, "add", "main/.gitignore")
    _git(checkout, "commit", "-m", "ignore fixture pollution")
    (checkout / "main/ignored-rogue.cc").write_text("local pollution\n", encoding="utf-8")

    with pytest.raises(module.OverlayTreeError, match="unexpected untracked main source"):
        module.verify_overlay_tree(checkout, integration)


def test_build_verifies_overlay_tree_before_idf_configuration() -> None:
    build_script = BUILD_SCRIPT.read_text(encoding="utf-8")

    apply_index = build_script.index('"apply-overlay.ps1"')
    verify_index = build_script.index('"verify-overlay-tree.py"')
    configure_index = build_script.index("set-target esp32s3")
    assert apply_index < verify_index < configure_index
