from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
from pathlib import Path

GENERATED_MAIN_PATHS = frozenset(
    {
        "main/assets/lang_config.h",
        "main/mmap_generate_emoji.h",
        "main/voice_agent_local_config.h",
    }
)


class OverlayTreeError(RuntimeError):
    pass


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    check: bool = True,
    input_data: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(command, cwd=cwd, env=env, input=input_data, capture_output=True, check=False)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).decode(errors="replace").strip()
        raise OverlayTreeError(f"command failed: {command[0]}: {detail or result.returncode}")
    return result


def _git_paths(checkout: Path, arguments: list[str], *, env: dict[str, str] | None = None) -> set[str]:
    output = _run(["git", *arguments, "-z", "--", "main"], cwd=checkout, env=env).stdout
    return {item.decode("utf-8") for item in output.split(b"\0") if item}


def _git_blob(checkout: Path, reference: str, *, env: dict[str, str] | None = None) -> bytes | None:
    exists = _run(["git", "cat-file", "-e", reference], cwd=checkout, env=env, check=False)
    if exists.returncode != 0:
        return None
    return _run(["git", "show", reference], cwd=checkout, env=env).stdout


def _content_oid(checkout: Path, path: str, content: bytes | None = None) -> bytes:
    command = ["git", "hash-object", f"--path={path}"]
    if content is None:
        command.append(path)
    else:
        command.append("--stdin")
    return _run(command, cwd=checkout, input_data=content).stdout.strip()


def _canonical_patch_state(checkout: Path, patches: list[Path]) -> dict[str, bytes | None]:
    with tempfile.NamedTemporaryFile(delete=False) as index_file:
        index_path = Path(index_file.name)
    index_path.unlink()
    env = os.environ.copy()
    env["GIT_INDEX_FILE"] = str(index_path)
    try:
        _run(["git", "read-tree", "HEAD"], cwd=checkout, env=env)
        for patch in patches:
            _run(["git", "apply", "--cached", str(patch)], cwd=checkout, env=env)
        changed_paths = _git_paths(checkout, ["diff", "--cached", "--name-only"], env=env)
        return {path: _git_blob(checkout, f":{path}", env=env) for path in changed_paths}
    finally:
        index_path.unlink(missing_ok=True)


def verify_overlay_tree(checkout: Path, integration: Path) -> None:
    checkout = checkout.resolve()
    integration = integration.resolve()
    if not (checkout / ".git").exists():
        raise OverlayTreeError(f"Xiaozhi checkout is not a Git worktree: {checkout}")

    patches = sorted((integration / "overlay").glob("*.patch"), key=lambda path: path.name)
    if not patches:
        raise OverlayTreeError(f"no canonical overlay patches found: {integration / 'overlay'}")
    desired = _canonical_patch_state(checkout, patches)

    overlay_files = integration / "overlay-files"
    if overlay_files.exists():
        for source in sorted(path for path in overlay_files.rglob("*") if path.is_file()):
            relative = source.relative_to(overlay_files).as_posix()
            if not relative.startswith("main/"):
                raise OverlayTreeError(f"overlay-files entry is outside main/: {relative}")
            desired[relative] = source.read_bytes()

    baseline_paths = _git_paths(checkout, ["ls-tree", "-r", "--name-only", "HEAD"])
    expected_tracked_changes: set[str] = set()
    expected_additions: set[str] = set()
    for path, expected_content in desired.items():
        baseline_content = _git_blob(checkout, f"HEAD:{path}")
        if path in baseline_paths:
            if expected_content != baseline_content:
                expected_tracked_changes.add(path)
        elif expected_content is not None:
            expected_additions.add(path)

    actual_tracked_changes = _git_paths(checkout, ["diff", "HEAD", "--name-only"])
    unexpected_tracked = actual_tracked_changes - expected_tracked_changes
    missing_tracked = expected_tracked_changes - actual_tracked_changes
    if unexpected_tracked:
        raise OverlayTreeError(
            "tracked main source differs outside canonical overlay: " + ", ".join(sorted(unexpected_tracked))
        )
    if missing_tracked:
        raise OverlayTreeError("canonical tracked overlay is missing: " + ", ".join(sorted(missing_tracked)))

    actual_untracked = _git_paths(checkout, ["ls-files", "--others"])
    unexpected_untracked = actual_untracked - expected_additions - GENERATED_MAIN_PATHS
    if unexpected_untracked:
        raise OverlayTreeError(
            "unexpected untracked main source outside canonical overlay: " + ", ".join(sorted(unexpected_untracked))
        )

    for path, expected_content in sorted(desired.items()):
        destination = checkout / Path(path)
        if expected_content is None:
            if destination.exists():
                raise OverlayTreeError(f"canonical overlay deletion was not applied: {path}")
        elif not destination.is_file():
            raise OverlayTreeError(f"canonical overlay file is missing: {path}")
        elif _content_oid(checkout, path) != _content_oid(checkout, path, expected_content):
            raise OverlayTreeError(f"canonical overlay content differs: {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the final Xiaozhi main/ tree against the canonical overlay.")
    integration = Path(__file__).resolve().parents[1]
    repo_root = integration.parents[2]
    parser.add_argument("--checkout", type=Path, default=repo_root / "external/xiaozhi-esp32")
    parser.add_argument("--integration", type=Path, default=integration)
    args = parser.parse_args()
    try:
        verify_overlay_tree(args.checkout, args.integration)
    except OverlayTreeError as error:
        parser.error(str(error))
    print("Canonical Xiaozhi main source overlay tree passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
