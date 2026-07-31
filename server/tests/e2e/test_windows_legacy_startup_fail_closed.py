from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import psutil
import pytest

SERVER_ROOT = Path(__file__).resolve().parents[2]
RUN_LOCAL = SERVER_ROOT / "scripts" / "run-local.ps1"


def _clean_process_environment() -> dict[str, str]:
    return {name: value for name, value in os.environ.items() if not name.upper().startswith("VOICE_")}


def _dotnet_ticks(timestamp: float) -> str:
    created_at = datetime.fromtimestamp(timestamp, tz=UTC)
    epoch = datetime(1, 1, 1, tzinfo=UTC)
    return str(int((created_at - epoch).total_seconds() * 10_000_000))


def _wait_for_child(path: Path, timeout: float = 5.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return int(path.read_text(encoding="ascii"))
        time.sleep(0.05)
    raise AssertionError("timed out waiting for legacy child PID")


def _same_process_is_alive(process_id: int, created_at: float) -> bool:
    try:
        process = psutil.Process(process_id)
        return process.is_running() and abs(process.create_time() - created_at) < 0.01
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
        return False


@pytest.mark.e2e_host
@pytest.mark.skipif(sys.platform != "win32", reason="Windows startup recovery test")
def test_run_local_fails_closed_for_plain_legacy_missing_root(tmp_path: Path) -> None:
    child_pid_file = tmp_path / "legacy-child.pid"
    root_program = (
        "import pathlib, subprocess, sys, threading; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding='ascii'); "
        "threading.Event().wait(60)"
    )
    root = subprocess.Popen(
        [sys._base_executable, "-c", root_program, str(child_pid_file)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    manifest_path = runtime_dir / "server-processes.json"
    child: psutil.Process | None = None
    cleanup_children: dict[tuple[int, float], psutil.Process] = {}
    try:
        child_id = _wait_for_child(child_pid_file)
        child = psutil.Process(child_id)
        child_created_at = child.create_time()
        root_process = psutil.Process(root.pid)
        root_created_at = root_process.create_time()
        root_executable = os.path.abspath(root_process.exe())

        root.terminate()
        root.wait(timeout=5)
        assert _same_process_is_alive(child_id, child_created_at)

        manifest_path.write_text(
            json.dumps(
                {
                    "processes": [
                        {
                            "name": "plain-legacy-missing-root",
                            "pid": root.pid,
                            "start_time_utc": datetime.fromtimestamp(
                                root_created_at, tz=UTC
                            ).isoformat(),
                            "start_time_utc_ticks": _dotnet_ticks(root_created_at),
                            "executable": root_executable,
                            "job_managed": False,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        for _ in range(2):
            started = subprocess.run(
                [
                    "pwsh",
                    "-NoProfile",
                    "-File",
                    str(RUN_LOCAL),
                    "-RuntimeDirectory",
                    str(runtime_dir),
                    "-EnvironmentFile",
                    str(tmp_path / "missing.env"),
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=20,
                env=_clean_process_environment(),
            )

            output = f"{started.stdout}\n{started.stderr}"
            assert started.returncode != 0
            assert "survivor identities remain" in output
            assert manifest_path.exists()
            retained = json.loads(manifest_path.read_text(encoding="utf-8"))
            assert len(retained["processes"]) == 1
            retained_entry = retained["processes"][0]
            assert retained_entry["legacy_descendant_scan_incomplete"] is True
            assert "legacy_termination_utc_ticks" not in retained_entry
            assert _same_process_is_alive(child_id, child_created_at)
    finally:
        if root.poll() is None:
            try:
                root_process = psutil.Process(root.pid)
                for descendant in root_process.children(recursive=True):
                    cleanup_children[(descendant.pid, descendant.create_time())] = descendant
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                pass
        if root.poll() is None:
            root.kill()
            root.wait(timeout=5)
        if child is not None:
            cleanup_children[(child.pid, child.create_time())] = child
        for (process_id, created_at), process in cleanup_children.items():
            try:
                if not _same_process_is_alive(process_id, created_at):
                    continue
                process.kill()
                process.wait(timeout=5)
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
        leaked = [
            process_id
            for process_id, created_at in cleanup_children
            if _same_process_is_alive(process_id, created_at)
        ]
        assert leaked == [], f"legacy startup test leaked process identities: {leaked}"
