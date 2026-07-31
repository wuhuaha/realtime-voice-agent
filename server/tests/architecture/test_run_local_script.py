from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run-local.ps1"
SUPERVISOR = Path(__file__).resolve().parents[2] / "scripts" / "windows_job_supervisor.py"
ROOT_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "run-local.ps1"
BOOTSTRAP_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "bootstrap.ps1"
HORIZONTAL_SCALE_SMOKE = Path(__file__).resolve().parents[1] / "e2e" / "test_horizontal_scale_processes.py"
CI_WORKFLOW = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "ci.yml"


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
    supervisor = SUPERVISOR.read_text(encoding="utf-8")
    assert "$WorkerBasePort + $index" in content
    assert "$UdpBasePort + $index" in content
    assert "VOICE_WORKER_ID" in content
    assert "VOICE_UDP_BIND_PORT" in content
    assert "VOICE_UDP_ADVERTISE_PORT" in content
    assert "VOICE_RVA_PUBLIC_WS_URL" in content
    assert "-WindowStyle Hidden" in content
    assert "server-processes.json" in content
    assert "VOICE_WORKER_BIND_HOST = $WorkerBindHost" in content
    assert "$publicRvaUriBuilder = [UriBuilder]$PublicRvaUri" in content
    assert "$publicRvaUriBuilder.Port = $workerPort" in content
    assert "$workerOverrides.VOICE_RVA_PUBLIC_WS_URL = $publicRvaUriBuilder.Uri.AbsoluteUri" in content
    assert "Wait-LocalHealth" in content
    assert "[int]$TimeoutSeconds = 60" in content
    assert "Write-Utf8NoBom" in content
    assert "ConvertFrom-Json -DateKind" not in content
    assert "Set-Content -LiteralPath $temporary -Encoding utf8NoBOM" not in content
    assert "start_time_utc" in content
    assert "start_time_utc_ticks" in content
    assert "OrdinalIgnoreCase" in content
    assert "$manifest.processes = @($survivors)" in content
    assert "retaining its recoverable identity" in content
    assert 'throw "Failed to stop $($survivors.Count)' in content
    assert "supervisor_pid" in content
    assert "supervisor_start_time_utc_ticks" in content
    assert "windows_job_supervisor.py" in content
    assert "job_managed" in content
    assert "Write-StartedManifest -StartupFailed $true" in content
    assert content.count("Stop-ProcessEntries") >= 3
    assert "Write-StartedManifest -StartupInProgress $true" in content
    assert "Resolve-RecordedStartTimeUtcTicks" in content
    assert "Get-LegacyProcessTreeSnapshot" not in content
    assert "Get-LegacyChildIdentityCapture" in content
    assert "legacy_descendant_scan_incomplete" in content
    assert "legacy_termination_utc_ticks" in content
    assert "$deadline = [DateTimeOffset]::UtcNow.AddSeconds(15)" in content
    assert "while ($madeProgress)" in content
    assert "-OperationTimeoutSec $operationTimeoutSeconds" in content
    assert "-TimeoutMilliseconds $remainingMilliseconds" in content
    assert "ProgressCallback" in content
    assert "Stop-LegacyRecordedProcessTrees" in content
    assert "Get-LegacyExactProcessGuard" in content
    assert "Close-LegacyExactProcessGuard" in content
    assert "ParentGuard" in content
    assert "$requiresLegacyRecovery" in content
    assert "if ($legacyStatus.state -ne 'match')" in content
    assert "Publish-LegacyRecoveryProgressAfterBound" in content
    assert "$state.retain_marker = $false" in content
    assert "$managedDeadline = [DateTimeOffset]::UtcNow.AddSeconds(15)" in content
    assert "-Deadline $managedDeadline" in content
    assert "exact survivor identities were retained" in content
    assert content.index("if ($requiresLegacyRecovery)") < content.index("$EnvFile = $EnvironmentFile")
    legacy_start = content.index("function Stop-LegacyRecordedProcessTrees")
    assert content.index("Publish-LegacyRecoveryProgress", legacy_start) < content.index(
        "$termination = Invoke-VerifiedTermination"
    )
    assert "termination_utc_ticks" in supervisor
    assert "process_absent_utc_ticks" in supervisor
    assert "inspection_started_utc_ticks" not in supervisor
    assert supervisor.index("handle = kernel32.OpenProcess(") < supervisor.index("process_absent_utc_ticks")
    legacy_section = content[
        content.index("function Get-LegacyIdentityKey {") : content.index("function Stop-ProcessEntries {")
    ]
    assert legacy_section.index("$beforeTermination = Get-LegacyChildIdentityCapture") < legacy_section.index(
        "$termination = Invoke-VerifiedTermination"
    )
    assert legacy_section.index("$guard = Get-LegacyExactProcessGuard") < legacy_section.index(
        "$beforeTermination = Get-LegacyChildIdentityCapture"
    )
    assert legacy_section.index("$state.terminated = $true") < legacy_section.index(
        "$afterTermination = Get-LegacyChildIdentityCapture"
    )
    assert "Start-Sleep" not in legacy_section
    assert "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE" in supervisor
    assert "CREATE_SUSPENDED" in supervisor
    assert "OpenProcess" in supervisor
    assert "GetProcessTimes" in supervisor
    assert "QueryFullProcessImageNameW" in supervisor
    assert "terminate_verified_process" in supervisor
    assert "_terminate_suspended_process" in supervisor
    assert "TerminateProcess(handle, 1)" in supervisor
    assert "TerminateProcess(process, 1)" in supervisor
    assert "_wait_for_process(kernel32, process, timeout_ms)" in supervisor
    assert "os.path.realpath" in supervisor
    assert "os.path.realpath(sys._base_executable)" in content
    assert supervisor.index("AssignProcessToJobObject(job, process.process)") < supervisor.index(
        "ResumeThread(process.thread)"
    )
    assert "Get-DescendantProcessIds" not in content
    assert "Stop-Process -Id $descendantIds" not in content
    for forbidden in ("taskkill", "/IM", "CTRL_C_EVENT", "WM_CLOSE", "CreateToolhelp32Snapshot", "CreateEventW"):
        assert forbidden not in content
    assert "[string]$EnvironmentFile" in content
    assert "$EnvironmentFile = [System.IO.Path]::GetFullPath($EnvironmentFile)" in content
    assert "$EnvFile = $EnvironmentFile" in content


def test_windows_ci_runs_legacy_process_tree_regressions() -> None:
    content = CI_WORKFLOW.read_text(encoding="utf-8")
    assert "windows-local-process-lifecycle-redis-e2e-not-covered" in content
    assert 'tests/e2e/test_horizontal_scale_processes.py' in content
    assert 'tests/e2e/test_windows_legacy_startup_fail_closed.py' in content
    assert '-k "run_local or windows_job_supervisor or legacy_manifest"' in content


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
