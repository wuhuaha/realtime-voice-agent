from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run-local.ps1"
ROOT_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "run-local.ps1"
BOOTSTRAP_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "bootstrap.ps1"
HORIZONTAL_SCALE_SMOKE = Path(__file__).resolve().parents[1] / "e2e" / "test_horizontal_scale_processes.py"


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
    assert "start_time_utc_ticks" in content
    assert "OrdinalIgnoreCase" in content
    assert "$manifest.processes = @($unmatched)" in content
    assert "Failed to stop recorded PID" in content
    assert "Get-DescendantProcessIds" in content
    assert "Get-CimInstance Win32_Process" in content
    assert "$ownedProcessIds" in content
    assert "[string]$EnvironmentFile" in content
    assert "$EnvironmentFile = [System.IO.Path]::GetFullPath($EnvironmentFile)" in content
    assert "$EnvFile = $EnvironmentFile" in content


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


def test_horizontal_scale_smoke_uses_real_processes_and_public_coordination_contracts() -> None:
    content = HORIZONTAL_SCALE_SMOKE.read_text(encoding="utf-8")
    assert "run-local.ps1" in content
    assert "VOICE_TEST_REDIS_URL" in content
    assert "VOICE_COORDINATION_BACKEND=redis" in content
    assert "VOICE_RUNNER=deterministic" in content
    assert '"/internal/v1/workers"' in content
    assert '"/v1/session/bootstrap"' in content
    assert "/internal/v1/drain" in content
    assert '"/internal/v1/grants/consume"' in content
    assert '"released_leases"' in content
