from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run-local.ps1"
ROOT_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "run-local.ps1"
BOOTSTRAP_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "bootstrap.ps1"


def test_run_local_script_parses_and_stop_is_idempotent(tmp_path: Path) -> None:
    parse = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-Command",
            (
                "$errors=$null; "
                f"[System.Management.Automation.Language.Parser]::ParseFile('{SCRIPT}',[ref]$null,[ref]$errors)"
                " | Out-Null; if($errors.Count){$errors | Out-String | Write-Error; exit 1}"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert parse.returncode == 0, parse.stderr

    stopped = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-File",
            str(SCRIPT),
            "-Stop",
            "-RuntimeDirectory",
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert stopped.returncode == 0, stopped.stderr


def test_run_local_assigns_unique_worker_and_udp_ports() -> None:
    content = SCRIPT.read_text(encoding="utf-8")
    assert "$WorkerBasePort + $index" in content
    assert "$UdpBasePort + $index" in content
    assert "VOICE_WORKER_ID" in content
    assert "VOICE_WORKER_PUBLIC_WS_URL" in content
    assert "VOICE_XIAOZHI_UDP_BIND_PORT" in content
    assert "-WindowStyle Hidden" in content
    assert "server-processes.json" in content
    assert "VOICE_WORKER_BIND_HOST = $WorkerBindHost" in content
    assert "VOICE_WORKER_PUBLIC_WS_URL" in content
    assert "Wait-LocalHealth" in content
    assert "start_time_utc" in content
    assert "OrdinalIgnoreCase" in content
    assert "$manifest.processes = @($unmatched)" in content


def test_root_launcher_forwards_topology_and_stop() -> None:
    content = ROOT_SCRIPT.read_text(encoding="utf-8")
    assert "-DirectorPort $DirectorPort" in content
    assert "-WorkerBasePort $WorkerBasePort" in content
    assert "-UdpBasePort $UdpBasePort" in content
    assert "-Stop:$Stop" in content


def test_bootstrap_installs_all_server_workspace_packages() -> None:
    content = BOOTSTRAP_SCRIPT.read_text(encoding="utf-8")
    assert "--directory (Join-Path $Root 'server') --locked --all-packages --dev" in content
    assert "Server dependency sync failed" in content
