from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import psutil
import pytest
from redis import Redis
from redis.exceptions import RedisError

SERVER_ROOT = Path(__file__).resolve().parents[2]
RUN_LOCAL = SERVER_ROOT / "scripts" / "run-local.ps1"
JOB_SUPERVISOR = SERVER_ROOT / "scripts" / "windows_job_supervisor.py"
INTERNAL_TOKEN = "validator-horizontal-scale-internal-token"
BOOTSTRAP_TOKEN = "validator-horizontal-scale-bootstrap-token"
SUPERVISOR_MODULE: Any | None = None


def _reserve_process_ports() -> tuple[int, int]:
    sockets: list[socket.socket] = []
    try:
        first_candidate = 20_000 + (uuid.uuid4().int % 3_000) * 3
        for index in range(3_000):
            director_port = 20_000 + ((first_candidate - 20_000 + index * 3) % 9_000)
            candidates = [socket.socket() for _ in range(3)]
            try:
                for offset, candidate in enumerate(candidates):
                    candidate.bind(("127.0.0.1", director_port + offset))
            except OSError:
                for candidate in candidates:
                    candidate.close()
                continue
            sockets.extend(candidates)
            return director_port, director_port + 1
        raise RuntimeError("could not reserve three contiguous process ports")
    finally:
        for reserved in sockets:
            reserved.close()


def _clean_process_environment() -> dict[str, str]:
    return {name: value for name, value in os.environ.items() if not name.upper().startswith("VOICE_")}


def _wait_for[T](probe: Any, *, timeout: float, description: str) -> T:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            value = probe()
            if value is not None:
                return value
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
        time.sleep(0.1)
    detail = f": {last_error}" if last_error is not None else ""
    raise AssertionError(f"timed out waiting for {description}{detail}")


def _supervisor_module() -> Any:
    global SUPERVISOR_MODULE  # noqa: PLW0603
    if SUPERVISOR_MODULE is not None:
        return SUPERVISOR_MODULE
    spec = importlib.util.spec_from_file_location("windows_job_supervisor_for_tests", JOB_SUPERVISOR)
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not load {JOB_SUPERVISOR}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    SUPERVISOR_MODULE = module
    return module


def _exact_process_identity(process_id: int) -> tuple[int, str]:
    supervisor = _supervisor_module()
    kernel32 = supervisor._kernel32()  # noqa: SLF001
    handle = kernel32.OpenProcess(supervisor.PROCESS_QUERY_LIMITED_INFORMATION, False, process_id)
    if not handle:
        raise ProcessLookupError(process_id)
    try:
        return supervisor._process_identity(kernel32, handle)  # noqa: SLF001
    finally:
        kernel32.CloseHandle(handle)


def _declared_process_executable(process: psutil.Process, fallback: object) -> str:
    try:
        command_line = process.cmdline()
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
        command_line = []
    if command_line:
        first = str(command_line[0])
        if first:
            return os.path.abspath(first)
    return str(fallback)


def _workers(client: httpx.Client) -> dict[str, dict[str, Any]] | None:
    response = client.get("/internal/v1/workers", headers={"X-Internal-Token": INTERNAL_TOKEN})
    if response.status_code != 200:
        return None
    workers = response.json()
    if len(workers) != 2:
        return None
    return {worker["worker_id"]: worker for worker in workers}


def _windows_process_snapshot(
    *,
    process_ids: Iterable[int] = (),
    parent_process_ids: Iterable[int] = (),
) -> dict[int, dict[str, Any]]:
    target_pids = {int(process_id) for process_id in process_ids}
    target_parent_pids = {int(process_id) for process_id in parent_process_ids}
    if not target_pids and not target_parent_pids:
        raise ValueError("process_ids or parent_process_ids is required")

    snapshot: dict[int, dict[str, Any]] = {}
    for process in psutil.process_iter(["pid", "ppid", "create_time", "exe"]):
        try:
            info = process.info
            process_id = int(info["pid"])
            parent_process_id = int(info["ppid"])
            if process_id not in target_pids and parent_process_id not in target_parent_pids:
                continue
            try:
                creation_ticks, executable = _exact_process_identity(process_id)
            except (OSError, ProcessLookupError):
                creation_ticks = _datetime_to_dotnet_ticks(datetime.fromtimestamp(float(info["create_time"]), tz=UTC))
                executable = info.get("exe")
            executable = _declared_process_executable(process, executable)
            snapshot[process_id] = {
                "ProcessId": process_id,
                "ParentProcessId": parent_process_id,
                "CreationDate": str(creation_ticks),
                "ExecutablePath": executable,
            }
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
    return snapshot


def _datetime_to_dotnet_ticks(value: datetime) -> int:
    timestamp = value.astimezone(UTC)
    return int((timestamp - datetime(1, 1, 1, tzinfo=UTC)).total_seconds() * 10_000_000)


def _dotnet_ticks_to_datetime(value: int) -> datetime:
    seconds, ticks = divmod(value, 10_000_000)
    return datetime(1, 1, 1, tzinfo=UTC) + timedelta(seconds=seconds, microseconds=ticks // 10)


def _dotnet_ticks_to_utc_roundtrip(value: int) -> str:
    created_at = _dotnet_ticks_to_datetime(value)
    subsecond_ticks = value % 10_000_000
    return f"{created_at:%Y-%m-%dT%H:%M:%S}.{subsecond_ticks:07d}Z"


def _cim_creation_to_utc_ticks(value: object) -> str:
    if isinstance(value, int) or (isinstance(value, str) and value.isdecimal()):
        return str(value)
    created_at = datetime.fromisoformat(str(value)).astimezone(UTC)
    return str(_datetime_to_dotnet_ticks(created_at))


def _process_identity_from_row(row: dict[str, Any]) -> dict[str, Any]:
    if str(row["CreationDate"]).isdecimal():
        start_time_utc = _dotnet_ticks_to_utc_roundtrip(int(row["CreationDate"]))
    else:
        created_at = datetime.fromisoformat(str(row["CreationDate"])).astimezone(UTC)
        start_time_utc = created_at.isoformat().replace("+00:00", "Z")
    return {
        "executable": str(row["ExecutablePath"]),
        "start_time_utc": start_time_utc,
        "start_time_utc_ticks": _cim_creation_to_utc_ticks(row["CreationDate"]),
    }


def _owned_process_identities(manifest: dict[str, Any]) -> dict[int, str]:
    owned: dict[int, str] = {}
    pending = [
        (int(entry["supervisor_pid"]), str(entry["supervisor_start_time_utc_ticks"]))
        for entry in manifest["processes"]
    ]
    while pending:
        process_id, expected_creation = pending.pop()
        snapshot = _windows_process_snapshot(process_ids=[process_id], parent_process_ids=[process_id])
        row = snapshot.get(process_id)
        if row is None or process_id in owned:
            continue
        actual_creation = _cim_creation_to_utc_ticks(row["CreationDate"])
        if actual_creation != expected_creation:
            continue
        owned[process_id] = actual_creation
        for child_id, child in snapshot.items():
            if int(child["ParentProcessId"]) != process_id:
                continue
            child_creation = _cim_creation_to_utc_ticks(child["CreationDate"])
            # Windows preserves PPID after a parent exits and may later reuse
            # that PID. A process older than this exact parent cannot be its child.
            if int(child_creation) >= int(actual_creation):
                pending.append((child_id, child_creation))
    return owned


def _matching_process_identities(identities: dict[int, str]) -> list[int]:
    snapshot = _windows_process_snapshot(process_ids=identities)
    return [
        process_id
        for process_id, creation_date in identities.items()
        if (row := snapshot.get(process_id)) is not None
        and _cim_creation_to_utc_ticks(row["CreationDate"]) == creation_date
    ]


def _ports_can_bind(ports: list[int]) -> bool:
    sockets: list[socket.socket] = []
    try:
        for port in ports:
            candidate = socket.socket()
            candidate.bind(("127.0.0.1", port))
            sockets.append(candidate)
    except OSError:
        return False
    finally:
        for candidate in sockets:
            candidate.close()
    return True


def _force_close_verified_supervisors(manifest: dict[str, Any]) -> None:
    for entry in manifest["processes"]:
        process_id = int(entry["supervisor_pid"])
        subprocess.run(
            [
                sys.executable,
                str(JOB_SUPERVISOR),
                "--terminate-pid",
                str(process_id),
                "--start-time-utc-ticks",
                str(entry["supervisor_start_time_utc_ticks"]),
                "--executable",
                str(entry["supervisor_executable"]),
                "--timeout-ms",
                "5000",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )


def _windows_process_identity(process_id: int) -> dict[str, Any]:
    row = _windows_process_snapshot(process_ids=[process_id]).get(process_id)
    if row is None:
        raise ProcessLookupError(process_id)
    return _process_identity_from_row(row)


def _wait_for_spawned_process_identity(
    process: subprocess.Popen[Any],
    *,
    stderr_path: Path,
) -> dict[str, Any]:
    def probe() -> dict[str, Any] | None:
        returncode = process.poll()
        if returncode is not None:
            stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
            raise AssertionError(
                "spawned process exited before its identity became available: "
                f"pid={process.pid}, returncode={returncode}, "
                f"stderr_path={stderr_path}, stderr={stderr!r}"
            )
        try:
            return _windows_process_identity(process.pid)
        except ProcessLookupError:
            return None

    return _wait_for(
        probe,
        timeout=2,
        description=f"spawned process {process.pid} identity",
    )


def _assert_manifest_process_identities(manifest: dict[str, Any]) -> None:
    process_ids = [
        int(process_id)
        for entry in manifest["processes"]
        for process_id in (entry["pid"], entry["supervisor_pid"])
    ]
    snapshot = _windows_process_snapshot(process_ids=process_ids)
    child_ids: set[int] = set()
    supervisor_ids: set[int] = set()
    for entry in manifest["processes"]:
        child_id = int(entry["pid"])
        supervisor_id = int(entry["supervisor_pid"])
        assert child_id > 0
        assert supervisor_id > 0
        assert child_id != supervisor_id
        assert child_id not in child_ids
        assert supervisor_id not in supervisor_ids
        child_ids.add(child_id)
        supervisor_ids.add(supervisor_id)

        child_row = snapshot.get(child_id)
        supervisor_row = snapshot.get(supervisor_id)
        assert child_row is not None
        assert supervisor_row is not None
        assert int(child_row["ParentProcessId"]) == supervisor_id

        child_identity = _windows_process_identity(child_id)
        supervisor_identity = _windows_process_identity(supervisor_id)
        assert child_identity["start_time_utc_ticks"] == entry["start_time_utc_ticks"]
        assert supervisor_identity["start_time_utc_ticks"] == entry["supervisor_start_time_utc_ticks"]
        assert os.path.normcase(os.path.realpath(child_identity["executable"])) == os.path.normcase(
            os.path.realpath(entry["executable"])
        )
        assert os.path.normcase(os.path.realpath(supervisor_identity["executable"])) == os.path.normcase(
            os.path.realpath(entry["supervisor_executable"])
        )


def _read_manifest_if_present(manifest_path: Path) -> dict[str, Any] | None:
    if not manifest_path.exists():
        return None
    return json.loads(manifest_path.read_text(encoding="utf-8-sig"))


def _invoke_stop(runtime_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-File",
            str(RUN_LOCAL),
            "-Stop",
            "-RuntimeDirectory",
            str(runtime_dir),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        env=_clean_process_environment(),
    )


def _run_legacy_stop_harness(
    tmp_path: Path,
    source: str,
    *,
    scenario: str,
) -> dict[str, Any]:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    harness = tmp_path / "legacy-stop-harness.ps1"
    harness.write_text(source, encoding="utf-8")
    completed = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-File",
            str(harness),
            "-RunLocal",
            str(RUN_LOCAL),
            "-RuntimeDirectory",
            str(runtime_dir),
            "-Scenario",
            scenario,
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        env=_clean_process_environment(),
    )
    assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}"
    result_lines = [line for line in completed.stdout.splitlines() if line.startswith("{")]
    assert result_lines, f"{completed.stdout}\n{completed.stderr}"
    return json.loads(result_lines[-1])


def _cluster_environment(
    worker_base_port: int,
    *,
    redis_url: str | None,
    redis_prefix: str | None,
) -> list[str]:
    environment = [
        "VOICE_ENV=development",
        "VOICE_ALLOW_SHARED_BOOTSTRAP_AUTH=true",
        "VOICE_ALLOW_LAB_AUTH=true",
        "VOICE_DIRECTOR_BIND_HOST=127.0.0.1",
        f"VOICE_INTERNAL_TOKEN={INTERNAL_TOKEN}",
        "VOICE_GRANT_SIGNING_KEY=validator-horizontal-scale-grant-signing-key",
        f"VOICE_DEVICE_BOOTSTRAP_TOKEN={BOOTSTRAP_TOKEN}",
        "VOICE_LAB_TOKEN=validator-horizontal-scale-lab-token",
        f"VOICE_RVA_PUBLIC_WS_URL=ws://127.0.0.1:{worker_base_port}/v2/voice",
        "VOICE_WORKER_BIND_HOST=127.0.0.1",
        "VOICE_HEARTBEAT_INTERVAL_SECONDS=1",
        "VOICE_RUNNER=deterministic",
        "VOICE_ROUTE_LEASE_TTL_SECONDS=5",
    ]
    if redis_url is None:
        environment.append("VOICE_COORDINATION_BACKEND=memory")
    else:
        assert redis_prefix is not None
        environment.extend(
            (
                "VOICE_COORDINATION_BACKEND=redis",
                f"VOICE_REDIS_URL={redis_url}",
                f"VOICE_COORDINATION_PREFIX={redis_prefix}",
            )
        )
    return environment


@contextmanager
def _running_cluster(
    tmp_path: Path,
    *,
    redis_url: str | None,
    redis_prefix: str | None,
) -> Iterator[dict[str, Any]]:
    director_port, worker_base_port = _reserve_process_ports()
    runtime_dir = tmp_path / "runtime with spaces"
    env_file = tmp_path / "horizontal-scale.env"
    environment = _cluster_environment(
        worker_base_port,
        redis_url=redis_url,
        redis_prefix=redis_prefix,
    )
    env_file.write_text("\n".join(environment) + "\n", encoding="utf-8")
    command = [
        "pwsh",
        "-NoProfile",
        "-File",
        str(RUN_LOCAL),
        "-WorkerCount",
        "2",
        "-RuntimeDirectory",
        str(runtime_dir),
        "-EnvironmentFile",
        str(env_file),
        "-DirectorPort",
        str(director_port),
        "-WorkerBasePort",
        str(worker_base_port),
        "-UdpBasePort",
        str(worker_base_port),
    ]
    launcher_stdout = tmp_path / "run-local.stdout.log"
    launcher_stderr = tmp_path / "run-local.stderr.log"
    manifest_path = runtime_dir / "server-processes.json"
    ports = [director_port, worker_base_port, worker_base_port + 1]
    manifest: dict[str, Any] | None = None
    owned_identities: dict[int, str] = {}
    supervisors: list[int] = []
    stopped: subprocess.CompletedProcess[str] | None = None
    stop_elapsed = 0.0
    primary_error: BaseException | None = None
    try:
        with launcher_stdout.open("w", encoding="utf-8") as stdout, launcher_stderr.open(
            "w", encoding="utf-8"
        ) as stderr:
            started = subprocess.run(
                command,
                check=False,
                stdout=stdout,
                stderr=stderr,
                text=True,
                timeout=45,
                env=_clean_process_environment(),
            )
        manifest = _read_manifest_if_present(manifest_path)
        assert started.returncode == 0, (
            f"{launcher_stdout.read_text(encoding='utf-8')}\n{launcher_stderr.read_text(encoding='utf-8')}"
        )
        assert manifest is not None
        assert manifest["startup_in_progress"] is False
        assert manifest["startup_failed"] is False
        assert all(process["job_managed"] is True for process in manifest["processes"])
        supervisors = [int(process["supervisor_pid"]) for process in manifest["processes"]]
        assert len(supervisors) == 3
        assert len(set(supervisors)) == 3
        _assert_manifest_process_identities(manifest)
        owned_identities = _owned_process_identities(manifest)
        yield manifest
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        snapshot_error: Exception | None = None
        recovery_manifest = _read_manifest_if_present(manifest_path) or manifest
        if recovery_manifest is not None and not owned_identities:
            try:
                owned_identities = _owned_process_identities(recovery_manifest)
            except Exception as exc:  # pragma: no cover - cleanup must continue after host inspection failure
                snapshot_error = exc
        stop_started = time.monotonic()
        stop_error: Exception | None = None
        try:
            stopped = _invoke_stop(runtime_dir)
        except Exception as exc:  # pragma: no cover - defensive cleanup after launcher failure
            stop_error = exc
        stop_elapsed = time.monotonic() - stop_started
        if stopped is None or stopped.returncode != 0:
            if recovery_manifest is not None:
                _force_close_verified_supervisors(recovery_manifest)
            try:
                _invoke_stop(runtime_dir)
            except Exception:
                pass
        try:
            process_survivors = _wait_for(
                lambda: [] if not _matching_process_identities(owned_identities) else None,
                timeout=5,
                description="Job Object process tree release",
            )
        except AssertionError as exc:
            survivors = _matching_process_identities(owned_identities)
            raise AssertionError(f"{exc}; surviving process identities: {survivors}") from exc
        released_ports = _wait_for(
            lambda: True if _ports_can_bind(ports) else None,
            timeout=5,
            description="local cluster port release",
        )
        if primary_error is None:
            assert snapshot_error is None, str(snapshot_error)
            assert set(supervisors) <= set(owned_identities)
            assert stop_error is None, str(stop_error)
            assert stopped is not None
            assert stopped.returncode == 0, f"{stopped.stdout}\n{stopped.stderr}"
            assert stop_elapsed < 20
            assert process_survivors == []
            assert released_ports is True
            assert not manifest_path.exists()


@pytest.mark.e2e_host
@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object lifecycle test")
def test_windows_job_supervisor_releases_descendants_and_ports(tmp_path: Path) -> None:
    with _running_cluster(tmp_path, redis_url=None, redis_prefix=None) as manifest:
        assert len(manifest["processes"]) == 3


@pytest.mark.e2e_host
@pytest.mark.skipif(sys.platform != "win32", reason="Windows process identity test")
def test_windows_job_supervisor_stop_failure_preserves_survivor_identity(tmp_path: Path) -> None:
    identity = _windows_process_identity(os.getpid())
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    manifest_path = runtime_dir / "server-processes.json"
    entry = {
        "name": "identity-negative-boundary",
        "pid": os.getpid(),
        "start_time_utc_ticks": identity["start_time_utc_ticks"],
        "executable": identity["executable"],
        "supervisor_pid": os.getpid(),
        "supervisor_start_time_utc_ticks": str(int(identity["start_time_utc_ticks"]) + 1),
        "supervisor_executable": identity["executable"],
        "job_managed": True,
    }
    manifest_path.write_text(json.dumps({"processes": [entry]}), encoding="utf-8")

    stopped = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-File",
            str(RUN_LOCAL),
            "-Stop",
            "-RuntimeDirectory",
            str(runtime_dir),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        env=_clean_process_environment(),
    )

    assert stopped.returncode != 0
    retained = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    assert retained["processes"] == [entry]


@pytest.mark.e2e_host
@pytest.mark.skipif(sys.platform != "win32", reason="Windows process identity helper test")
def test_windows_job_supervisor_missing_bound_is_recorded_after_inspection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = _supervisor_module()
    events: list[str] = []

    class MissingProcessKernel:
        def OpenProcess(self, access: int, inherit: bool, process_id: int) -> int:  # noqa: N802
            events.append("open-process")
            return 0

    def record_clock() -> int:
        events.append("clock")
        return 638000000000000123

    monkeypatch.setattr(supervisor, "_kernel32", MissingProcessKernel)
    monkeypatch.setattr(supervisor, "_utc_now_dotnet_ticks", record_clock)
    monkeypatch.setattr(supervisor.ctypes, "get_last_error", lambda: supervisor.ERROR_INVALID_PARAMETER)

    result = supervisor.terminate_verified_process(
        1234,
        638000000000000000,
        r"C:\tree\root.exe",
        start_time_tolerance_ticks=0,
        timeout_ms=5000,
    )

    assert events == ["open-process", "clock"]
    assert result == {
        "state": "missing",
        "process_id": 1234,
        "process_absent_utc_ticks": 638000000000000123,
    }


@pytest.mark.e2e_host
def test_spawned_process_identity_retries_initial_lookup_miss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr_path = tmp_path / "spawned.stderr.log"
    stderr_path.write_text("", encoding="utf-8")
    expected = {"start_time_utc_ticks": "638000000000000123"}
    events: list[str] = []

    class RunningProcess:
        pid = 1234

        def poll(self) -> None:
            events.append("poll")
            return None

    attempts = 0

    def identity(process_id: int) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        events.append(f"identity:{process_id}")
        if attempts == 1:
            raise ProcessLookupError(process_id)
        return expected

    monkeypatch.setitem(globals(), "_windows_process_identity", identity)
    monkeypatch.setattr(time, "sleep", lambda _: events.append("sleep"))

    assert (
        _wait_for_spawned_process_identity(RunningProcess(), stderr_path=stderr_path) == expected
    )
    assert events == ["poll", "identity:1234", "sleep", "poll", "identity:1234"]


@pytest.mark.e2e_host
def test_spawned_process_identity_reports_already_exited_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr_path = tmp_path / "spawned.stderr.log"
    stderr_path.write_text("fixture startup failed\n", encoding="utf-8")
    events: list[str] = []

    class ExitedProcess:
        pid = 4321

        def poll(self) -> int:
            events.append("poll")
            return 17

    monkeypatch.setitem(
        globals(),
        "_windows_process_identity",
        lambda _: pytest.fail("identity lookup must not run after process exit"),
    )
    monkeypatch.setattr(time, "sleep", lambda _: pytest.fail("exit diagnosis must not retry"))

    with pytest.raises(AssertionError) as exc_info:
        _wait_for_spawned_process_identity(ExitedProcess(), stderr_path=stderr_path)

    message = str(exc_info.value)
    assert "pid=4321" in message
    assert "returncode=17" in message
    assert f"stderr_path={stderr_path}" in message
    assert "fixture startup failed" in message
    assert events == ["poll"]


@pytest.mark.e2e_host
@pytest.mark.skipif(sys.platform != "win32", reason="Windows legacy process lifecycle test")
def test_legacy_manifest_start_time_fallback_remains_stoppable(tmp_path: Path) -> None:
    process = subprocess.Popen(
        [sys._base_executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    manifest_path = runtime_dir / "server-processes.json"
    try:
        identity = _windows_process_identity(process.pid)
        manifest_path.write_text(
            json.dumps(
                {
                    "processes": [
                        {
                            "name": "legacy-start-time-fallback",
                            "pid": process.pid,
                            "start_time_utc": identity["start_time_utc"],
                            "executable": identity["executable"],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        stopped = _invoke_stop(runtime_dir)

        assert process.wait(timeout=5) != 0
        if stopped.returncode == 0:
            assert not manifest_path.exists()
        else:
            assert "survivor identities remain" in stopped.stderr
            retained = json.loads(manifest_path.read_text(encoding="utf-8"))
            entries = retained["processes"]
            assert entries
            assert all(
                entry.get("legacy_descendant_scan_incomplete") is True
                for entry in entries
            )
            assert all(int(entry["pid"]) != process.pid for entry in entries)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


LEGACY_PROCESS_TREE_HARNESS = r"""
param(
    [string]$RunLocal,
    [string]$RuntimeDirectory,
    [ValidateSet(
        'fixed-point',
        'pre-capture-failure',
        'post-capture-failure',
        'post-termination-exception',
        'journal-write-failure',
        'bound-journal-write-failure',
        'plain-root-missing',
        'parent-pid-reuse-before-capture',
        'missing-late-child',
        'missing-pid-reuse',
        'bounded-parent-unbounded-child'
    )]
    [string]$Scenario
)
. $RunLocal -Stop -RuntimeDirectory $RuntimeDirectory

$baseTicks = 638000000000000000L
$script:processes = @{}
$script:terminationStates = @{}
$script:terminationBounds = @{}
$script:spawnOnTermination = @{}
$script:hiddenCaptures = @{}
$script:captureFailures = @{}
$script:postTerminationCaptureFailures = @{}
$script:attemptOrder = [System.Collections.Generic.List[int]]::new()
$script:allowRecovery = $false
$script:journalWrites = 0
$script:reuseInjected = $false

function Add-FakeProcess {
    param(
        [int]$ProcessId,
        [int]$ParentProcessId,
        [long]$CreatedTicks,
        [string]$Executable,
        [bool]$Alive
    )
    $script:processes[$ProcessId] = [pscustomobject]@{
        ProcessId = $ProcessId
        ParentProcessId = $ParentProcessId
        CreationDate = [DateTime]::new($CreatedTicks, [DateTimeKind]::Utc)
        ExecutablePath = $Executable
        alive = $Alive
    }
}

function Set-FakeTermination {
    param(
        [int]$ProcessId,
        [string]$State,
        [long]$BoundTicks,
        [int[]]$SpawnIds = @()
    )
    $script:terminationStates[$ProcessId] = $State
    $script:terminationBounds[$ProcessId] = $BoundTicks
    $script:spawnOnTermination[$ProcessId] = @($SpawnIds)
}

switch ($Scenario) {
    'fixed-point' {
        Add-FakeProcess 100 0 $baseTicks 'C:\tree\root.exe' $true
        Add-FakeProcess 101 100 ($baseTicks + 100) 'C:\tree\child.exe' $true
        Add-FakeProcess 102 101 ($baseTicks + 300) 'C:\tree\grandchild.exe' $false
        Add-FakeProcess 103 102 ($baseTicks + 500) 'C:\tree\great-grandchild.exe' $false
        Add-FakeProcess 900 100 ($baseTicks + 900) 'C:\unrelated\python.exe' $false
        Set-FakeTermination 100 'terminated' ($baseTicks + 250) @(900)
        Set-FakeTermination 101 'terminated' ($baseTicks + 350) @(102)
        Set-FakeTermination 102 'terminated' ($baseTicks + 550) @(103)
        Set-FakeTermination 103 'terminated' ($baseTicks + 650)
        $script:hiddenCaptures[101] = 1
        $script:hiddenCaptures[102] = 1
        $rootId = 100
    }
    'pre-capture-failure' {
        Add-FakeProcess 200 0 $baseTicks 'C:\tree\root.exe' $true
        Set-FakeTermination 200 'terminated' ($baseTicks + 100)
        $script:captureFailures[200] = 1
        $rootId = 200
    }
    'post-capture-failure' {
        Add-FakeProcess 300 0 $baseTicks 'C:\tree\root.exe' $true
        Add-FakeProcess 301 300 ($baseTicks + 100) 'C:\tree\late-child.exe' $false
        Set-FakeTermination 300 'terminated' ($baseTicks + 200) @(301)
        Set-FakeTermination 301 'terminated' ($baseTicks + 300)
        $script:postTerminationCaptureFailures[300] = 1
        $rootId = 300
    }
    'post-termination-exception' {
        Add-FakeProcess 300 0 $baseTicks 'C:\tree\root.exe' $true
        Add-FakeProcess 301 300 ($baseTicks + 100) 'C:\tree\late-child.exe' $false
        Set-FakeTermination 300 'terminated' ($baseTicks + 200) @(301)
        Set-FakeTermination 301 'terminated' ($baseTicks + 300)
        $rootId = 300
    }
    'journal-write-failure' {
        Add-FakeProcess 600 0 $baseTicks 'C:\tree\root.exe' $true
        Add-FakeProcess 601 600 ($baseTicks + 100) 'C:\tree\late-child.exe' $false
        Set-FakeTermination 600 'terminated' ($baseTicks + 200) @(601)
        Set-FakeTermination 601 'terminated' ($baseTicks + 300)
        $rootId = 600
    }
    'bound-journal-write-failure' {
        Add-FakeProcess 600 0 $baseTicks 'C:\tree\root.exe' $true
        Add-FakeProcess 601 600 ($baseTicks + 100) 'C:\tree\late-child.exe' $false
        Set-FakeTermination 600 'terminated' ($baseTicks + 200) @(601)
        Set-FakeTermination 601 'terminated' ($baseTicks + 300)
        $rootId = 600
    }
    'plain-root-missing' {
        Add-FakeProcess 800 0 $baseTicks 'C:\tree\missing-root.exe' $false
        $rootId = 800
    }
    'parent-pid-reuse-before-capture' {
        Add-FakeProcess 700 0 $baseTicks 'C:\tree\original-root.exe' $true
        Add-FakeProcess 701 700 ($baseTicks + 100) 'C:\unrelated\child.exe' $false
        $rootId = 700
    }
    'missing-late-child' {
        Add-FakeProcess 400 0 $baseTicks 'C:\tree\root.exe' $true
        Add-FakeProcess 401 400 ($baseTicks + 100) 'C:\tree\candidate-child.exe' $false
        Set-FakeTermination 400 'missing' ($baseTicks + 200) @(401)
        Set-FakeTermination 401 'terminated' ($baseTicks + 400)
        $rootId = 400
    }
    'missing-pid-reuse' {
        Add-FakeProcess 400 0 $baseTicks 'C:\tree\root.exe' $true
        Add-FakeProcess 401 400 ($baseTicks + 300) 'C:\unrelated\candidate-child.exe' $false
        Set-FakeTermination 400 'missing' ($baseTicks + 200) @(401)
        Set-FakeTermination 401 'terminated' ($baseTicks + 400)
        $rootId = 400
    }
    'bounded-parent-unbounded-child' {
        Add-FakeProcess 500 0 $baseTicks 'C:\tree\root.exe' $false
        Add-FakeProcess 501 500 ($baseTicks + 100) 'C:\tree\child.exe' $true
        Add-FakeProcess 502 501 ($baseTicks + 250) 'C:\tree\grandchild.exe' $false
        Add-FakeProcess 599 501 ($baseTicks + 500) 'C:\unrelated\reused-child.exe' $false
        Set-FakeTermination 501 'terminated' ($baseTicks + 400) @(502, 599)
        Set-FakeTermination 502 'terminated' ($baseTicks + 450)
        $rootId = 500
    }
}

function Get-RecordedProcessStatus {
    param(
        [int]$ProcessId,
        [string]$StartTimeUtcTicks,
        [object]$StartTimeUtc,
        [int]$StartTimeToleranceTicks = 0,
        [string]$Executable
    )
    if ($Scenario -eq 'parent-pid-reuse-before-capture' -and
        $ProcessId -eq 700 -and
        -not $script:reuseInjected) {
        $script:reuseInjected = $true
        Add-FakeProcess 700 0 ($baseTicks + 50) 'C:\unrelated\reused-parent.exe' $true
        $script:processes[701].alive = $true
        return [pscustomobject]@{ state = 'match'; process = $null }
    }
    $process = $script:processes[$ProcessId]
    if ($null -ne $process -and $process.alive) {
        return [pscustomobject]@{ state = 'match'; process = $null }
    }
    return [pscustomobject]@{ state = 'missing'; process = $null }
}

function Get-LegacyExactProcessGuard {
    param([object]$Identity)
    if ($Scenario -eq 'parent-pid-reuse-before-capture' -and
        [int]$Identity.pid -eq 700) {
        if (-not $script:reuseInjected) {
            $script:reuseInjected = $true
            Add-FakeProcess 700 0 ($baseTicks + 50) 'C:\unrelated\reused-parent.exe' $true
            $script:processes[701].alive = $true
        }
        return [pscustomobject]@{
            state = 'mismatch'
            process = $null
            identity_key = Get-LegacyIdentityKey -Identity $Identity
        }
    }
    $process = $script:processes[[int]$Identity.pid]
    if ($null -ne $process -and $process.alive) {
        return [pscustomobject]@{
            state = 'match'
            process = $process
            identity_key = Get-LegacyIdentityKey -Identity $Identity
        }
    }
    return [pscustomobject]@{
        state = 'missing'
        process = $null
        identity_key = Get-LegacyIdentityKey -Identity $Identity
    }
}

function Close-LegacyExactProcessGuard {
    param([object]$Guard)
}

function Get-CimInstance {
    param(
        [string]$ClassName,
        [string]$Filter,
        [uint]$OperationTimeoutSec,
        [object]$ErrorAction
    )
    $parentId = [int]([regex]::Match($Filter, '(\d+)$').Groups[1].Value)
    if ([int]$script:captureFailures[$parentId] -gt 0) {
        $script:captureFailures[$parentId] = [int]$script:captureFailures[$parentId] - 1
        throw 'injected pre-termination CIM capture failure'
    }
    if (-not $script:processes[$parentId].alive -and
        [int]$script:postTerminationCaptureFailures[$parentId] -gt 0) {
        $script:postTerminationCaptureFailures[$parentId] = [int]$script:postTerminationCaptureFailures[$parentId] - 1
        throw 'injected post-termination CIM capture failure'
    }
    $candidates = @(
        $script:processes.Values |
            Where-Object { $_.alive -and $_.ParentProcessId -eq $parentId } |
            Sort-Object ProcessId
    )
    if ($candidates.Count -gt 0 -and [int]$script:hiddenCaptures[$parentId] -gt 0) {
        $script:hiddenCaptures[$parentId] = [int]$script:hiddenCaptures[$parentId] - 1
        return @()
    }
    return @($candidates)
}

$script:originalLegacyCapture = ${function:Get-LegacyChildIdentityCapture}
function Get-LegacyChildIdentityCapture {
    param(
        [object]$ParentIdentity,
        [object]$CreatedNotAfterTicks,
        [DateTimeOffset]$Deadline,
        [string]$RootName,
        [object]$ParentGuard = $null
    )
    if ($Scenario -eq 'post-termination-exception' -and
        [int]$ParentIdentity.pid -eq 300 -and
        -not $script:processes[300].alive -and
        -not $script:allowRecovery) {
        throw 'injected unhandled post-termination exception'
    }
    & $script:originalLegacyCapture @PSBoundParameters
}

$script:originalWriteJsonAtomically = ${function:Write-JsonAtomically}
function Write-JsonAtomically {
    param([object]$Value)
    $script:journalWrites += 1
    if ($Scenario -eq 'journal-write-failure' -and -not $script:allowRecovery -and $script:journalWrites -eq 1) {
        throw 'injected journal write failure'
    }
    if ($Scenario -eq 'bound-journal-write-failure' -and -not $script:allowRecovery -and $script:journalWrites -eq 2) {
        throw 'injected bound journal write failure'
    }
    & $script:originalWriteJsonAtomically -Value $Value
}

function Invoke-VerifiedTermination {
    param(
        [int]$ProcessId,
        [string]$StartTimeUtcTicks,
        [object]$StartTimeUtc,
        [int]$StartTimeToleranceTicks = 0,
        [string]$Executable,
        [int]$TimeoutMilliseconds = 5000
    )
    $script:attemptOrder.Add($ProcessId)
    $process = $script:processes[$ProcessId]
    if ($null -eq $process -or -not $process.alive) {
        return [pscustomobject]@{
            state = 'missing'
            process_id = $ProcessId
            process_absent_utc_ticks = [string]($baseTicks + 1000)
        }
    }
    if ($Scenario -eq 'parent-pid-reuse-before-capture' -and $ProcessId -eq 700) {
        return [pscustomobject]@{ state = 'mismatch'; process_id = $ProcessId }
    }
    $process.alive = $false
    foreach ($spawnId in @($script:spawnOnTermination[$ProcessId])) {
        $script:processes[[int]$spawnId].alive = $true
    }
    $state = [string]$script:terminationStates[$ProcessId]
    $bound = [string]$script:terminationBounds[$ProcessId]
    if ($state -eq 'missing') {
        return [pscustomobject]@{
            state = 'missing'
            process_id = $ProcessId
            process_absent_utc_ticks = $bound
        }
    }
    return [pscustomobject]@{
        state = 'terminated'
        process_id = $ProcessId
        termination_utc_ticks = $bound
    }
}

function New-RootEntry {
    $root = $script:processes[$rootId]
    return [pscustomobject]@{
        name = "legacy-$Scenario"
        pid = $rootId
        start_time_utc_ticks = [string]$root.CreationDate.Ticks
        executable = [string]$root.ExecutablePath
    }
}

function Write-FakeManifest {
    param([object]$Entry)
    $manifestPath = Join-Path $RuntimeDirectory 'server-processes.json'
    [System.IO.File]::WriteAllText(
        $manifestPath,
        (@{ processes = @($Entry) } | ConvertTo-Json -Depth 4),
        [System.Text.UTF8Encoding]::new($false)
    )
    return $manifestPath
}

if ($Scenario -eq 'bounded-parent-unbounded-child') {
    $root = New-RootEntry
    $parentMarker = ConvertTo-LegacyScanMarker `
        -Identity $root `
        -TerminationUtcTicks ($baseTicks + 200)
    $child = $script:processes[501]
    $childMarker = ConvertTo-LegacyScanMarker -Identity ([pscustomobject]@{
        name = 'legacy-bounded-parent-unbounded-child-descendant-501'
        pid = 501
        start_time_utc_ticks = [string]$child.CreationDate.Ticks
        executable = [string]$child.ExecutablePath
    }) -TerminationUtcTicks $null
    $manifestPath = Join-Path $RuntimeDirectory 'server-processes.json'
    [System.IO.File]::WriteAllText(
        $manifestPath,
        (@{ processes = @($parentMarker, $childMarker) } | ConvertTo-Json -Depth 6),
        [System.Text.UTF8Encoding]::new($false)
    )
    $firstFailed = $false
    try { Stop-RecordedProcesses } catch { $firstFailed = $true }
    $secondFailed = $false
    try { Stop-RecordedProcesses } catch { $secondFailed = $true }
    [pscustomobject]@{
        first_failed = $firstFailed
        second_failed = $secondFailed
        manifest_exists = Test-Path -LiteralPath $manifestPath
        attempt_order = @($script:attemptOrder)
        alive_pids = @(
            $script:processes.Values |
                Where-Object { $_.alive } |
                ForEach-Object { $_.ProcessId } |
                Sort-Object
        )
    } | ConvertTo-Json -Compress
    return
}

if ($Scenario -eq 'parent-pid-reuse-before-capture') {
    $manifestPath = Write-FakeManifest -Entry (New-RootEntry)
    $failed = $false
    try { Stop-RecordedProcesses } catch { $failed = $true }
    $retained = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
    [pscustomobject]@{
        failed = $failed
        retained_marker = [bool]$retained.processes[0].legacy_descendant_scan_incomplete
        retained_process_count = @($retained.processes).Count
        manifest_exists = Test-Path -LiteralPath $manifestPath
        attempt_order = @($script:attemptOrder)
        alive_pids = @(
            $script:processes.Values |
                Where-Object { $_.alive } |
                ForEach-Object { $_.ProcessId } |
                Sort-Object
        )
    } | ConvertTo-Json -Compress
    return
}

if ($Scenario -in @('fixed-point', 'missing-late-child', 'missing-pid-reuse')) {
    $stopResult = Stop-LegacyRecordedProcessTree -Entry (New-RootEntry)
    $alivePids = @(
        $script:processes.Values |
            Where-Object { $_.alive } |
            ForEach-Object { $_.ProcessId } |
            Sort-Object
    )
    [pscustomobject]@{
        survivors = @($stopResult.survivors).Count
        attempt_order = @($script:attemptOrder)
        alive_pids = $alivePids
    } | ConvertTo-Json -Compress
    return
}

$manifestPath = Write-FakeManifest -Entry (New-RootEntry)
$firstFailed = $false
try {
    Stop-RecordedProcesses
}
catch {
    $firstFailed = $true
}
$retained = if (Test-Path -LiteralPath $manifestPath) {
    Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
}
else {
    [pscustomobject]@{ processes = @() }
}
$retainedEntry = @($retained.processes | Select-Object -First 1)
$rootAliveAfterFirst = $script:processes[$rootId].alive
$childId = if ($rootId -eq 600) { 601 } else { 301 }
$recoverableChildScenarios = @(
    'post-capture-failure',
    'post-termination-exception',
    'journal-write-failure',
    'bound-journal-write-failure'
)
$childAliveAfterFirst = if ($Scenario -in $recoverableChildScenarios) {
    $script:processes[$childId].alive
}
else {
    $false
}
$script:allowRecovery = $true
$secondFailed = $false
try {
    Stop-RecordedProcesses
}
catch {
    $secondFailed = $true
}
[pscustomobject]@{
    first_failed = $firstFailed
    root_alive_after_first = $rootAliveAfterFirst
    child_alive_after_first = $childAliveAfterFirst
    retained_marker = [bool]$retainedEntry.legacy_descendant_scan_incomplete
    retained_bound = [string]$retainedEntry.legacy_termination_utc_ticks
    retained_process_count = @($retained.processes).Count
    second_failed = $secondFailed
    child_alive_after_second = if ($Scenario -in $recoverableChildScenarios) {
        $script:processes[$childId].alive
    }
    else {
        $false
    }
    manifest_exists_after_second = Test-Path -LiteralPath $manifestPath
    attempt_order = @($script:attemptOrder)
} | ConvertTo-Json -Compress
"""

MANAGED_DEADLINE_HARNESS = r"""
param(
    [string]$RunLocal,
    [string]$RuntimeDirectory,
    [string]$Scenario
)
. $RunLocal -Stop -RuntimeDirectory $RuntimeDirectory

$script:remainingCalls = 0
$script:attempts = [System.Collections.Generic.List[int]]::new()
function Get-LegacyRemainingTimeoutMilliseconds {
    param([DateTimeOffset]$Deadline, [int]$Maximum = 5000)
    $script:remainingCalls += 1
    if ($script:remainingCalls -le 2) {
        return 1
    }
    return 0
}
function Invoke-VerifiedTermination {
    param(
        [int]$ProcessId,
        [string]$StartTimeUtcTicks,
        [object]$StartTimeUtc,
        [int]$StartTimeToleranceTicks = 0,
        [string]$Executable,
        [int]$TimeoutMilliseconds = 5000
    )
    $script:attempts.Add($ProcessId)
    return [pscustomobject]@{ state = 'timeout'; process_id = $ProcessId }
}

$entries = @(
    1..32 | ForEach-Object {
        [pscustomobject]@{
            name = "managed-$_"
            pid = 2000 + $_
            start_time_utc_ticks = [string](638000000000000000L + $_)
            executable = 'C:\tree\child.exe'
            supervisor_pid = 1000 + $_
            supervisor_start_time_utc_ticks = [string](638000000000001000L + $_)
            supervisor_executable = 'C:\tree\supervisor.exe'
            job_managed = $true
        }
    }
)
$result = Stop-ProcessEntries -Entries $entries
[pscustomobject]@{
    survivor_count = @($result.survivors).Count
    survivor_names = @($result.survivors | ForEach-Object { $_.name })
    attempts = @($script:attempts)
    remaining_calls = $script:remainingCalls
} | ConvertTo-Json -Compress
"""


@pytest.mark.e2e_host
@pytest.mark.skipif(sys.platform != "win32", reason="Windows managed cleanup deadline test")
def test_run_local_managed_cleanup_uses_one_shared_deadline(tmp_path: Path) -> None:
    result = _run_legacy_stop_harness(
        tmp_path,
        MANAGED_DEADLINE_HARNESS,
        scenario="managed-deadline",
    )
    assert result["survivor_count"] == 32
    assert result["survivor_names"] == [f"managed-{index}" for index in range(1, 33)]
    assert result["attempts"] == [1001]
    assert result["remaining_calls"] == 34


@pytest.mark.e2e_host
@pytest.mark.skipif(sys.platform != "win32", reason="Windows legacy missing-parent bound test")
@pytest.mark.parametrize(
    ("scenario", "expected_alive_pids", "expected_attempt_order"),
    [
        pytest.param("missing-late-child", [], [400, 401], id="missing-late-child"),
        pytest.param("missing-pid-reuse", [401], [400], id="missing-pid-reuse"),
    ],
)
def test_legacy_manifest_missing_parent_uses_inspection_bound(
    tmp_path: Path,
    scenario: str,
    expected_alive_pids: list[int],
    expected_attempt_order: list[int],
) -> None:
    result = _run_legacy_stop_harness(
        tmp_path,
        LEGACY_PROCESS_TREE_HARNESS,
        scenario=scenario,
    )
    assert result == {
        "survivors": 0,
        "attempt_order": expected_attempt_order,
        "alive_pids": expected_alive_pids,
    }


@pytest.mark.e2e_host
@pytest.mark.skipif(sys.platform != "win32", reason="Windows legacy fixed-point process tree test")
def test_legacy_manifest_fixed_point_stops_late_multigeneration_descendants(
    tmp_path: Path,
) -> None:
    result = _run_legacy_stop_harness(
        tmp_path,
        LEGACY_PROCESS_TREE_HARNESS,
        scenario="fixed-point",
    )
    assert result == {
        "survivors": 0,
        "attempt_order": [100, 101, 102, 103],
        "alive_pids": [900],
    }


@pytest.mark.e2e_host
@pytest.mark.skipif(sys.platform != "win32", reason="Windows legacy marker merge test")
def test_legacy_manifest_merges_parent_replay_with_live_child_marker(tmp_path: Path) -> None:
    result = _run_legacy_stop_harness(
        tmp_path,
        LEGACY_PROCESS_TREE_HARNESS,
        scenario="bounded-parent-unbounded-child",
    )
    assert result == {
        "first_failed": False,
        "second_failed": False,
        "manifest_exists": False,
        "attempt_order": [501, 502],
        "alive_pids": [599],
    }


@pytest.mark.e2e_host
@pytest.mark.skipif(sys.platform != "win32", reason="Windows legacy progress journal test")
@pytest.mark.parametrize(
    ("scenario", "root_alive_after_first", "retained_bound", "attempt_order"),
    [
        pytest.param("post-termination-exception", False, "638000000000000200", [300, 301]),
        pytest.param("journal-write-failure", True, "", [600, 601]),
    ],
)
def test_legacy_manifest_journals_each_recovery_transition(
    tmp_path: Path,
    scenario: str,
    root_alive_after_first: bool,
    retained_bound: str,
    attempt_order: list[int],
) -> None:
    result = _run_legacy_stop_harness(tmp_path, LEGACY_PROCESS_TREE_HARNESS, scenario=scenario)
    assert result == {
        "first_failed": True,
        "root_alive_after_first": root_alive_after_first,
        "child_alive_after_first": scenario == "post-termination-exception",
        "retained_marker": scenario == "post-termination-exception",
        "retained_bound": retained_bound,
        "retained_process_count": 1,
        "second_failed": False,
        "child_alive_after_second": False,
        "manifest_exists_after_second": False,
        "attempt_order": attempt_order,
    }


@pytest.mark.e2e_host
@pytest.mark.skipif(sys.platform != "win32", reason="Windows legacy exact parent guard test")
def test_legacy_manifest_rejects_children_captured_after_parent_pid_reuse(tmp_path: Path) -> None:
    result = _run_legacy_stop_harness(
        tmp_path,
        LEGACY_PROCESS_TREE_HARNESS,
        scenario="parent-pid-reuse-before-capture",
    )
    assert result == {
        "failed": True,
        "retained_marker": True,
        "retained_process_count": 1,
        "manifest_exists": True,
        "attempt_order": [],
        "alive_pids": [700, 701],
    }


@pytest.mark.e2e_host
@pytest.mark.skipif(sys.platform != "win32", reason="Windows legacy fail-closed marker test")
@pytest.mark.parametrize(
    ("scenario", "root_alive_after_first", "child_alive_after_first", "attempt_order"),
    [pytest.param("plain-root-missing", False, False, [], id="plain-root-missing")],
)
def test_legacy_manifest_unbounded_recovery_fails_closed_across_retries(
    tmp_path: Path,
    scenario: str,
    root_alive_after_first: bool,
    child_alive_after_first: bool,
    attempt_order: list[int],
) -> None:
    result = _run_legacy_stop_harness(
        tmp_path,
        LEGACY_PROCESS_TREE_HARNESS,
        scenario=scenario,
    )
    assert result == {
        "first_failed": True,
        "root_alive_after_first": root_alive_after_first,
        "child_alive_after_first": child_alive_after_first,
        "retained_marker": True,
        "retained_bound": "",
        "retained_process_count": 1,
        "second_failed": True,
        "child_alive_after_second": child_alive_after_first,
        "manifest_exists_after_second": True,
        "attempt_order": attempt_order,
    }


@pytest.mark.e2e_host
@pytest.mark.skipif(sys.platform != "win32", reason="Windows bounded journal retry test")
def test_legacy_manifest_bound_journal_failure_recovers_in_same_stop(tmp_path: Path) -> None:
    result = _run_legacy_stop_harness(
        tmp_path,
        LEGACY_PROCESS_TREE_HARNESS,
        scenario="bound-journal-write-failure",
    )
    assert result == {
        "first_failed": False,
        "root_alive_after_first": False,
        "child_alive_after_first": False,
        "retained_marker": False,
        "retained_bound": "",
        "retained_process_count": 0,
        "second_failed": False,
        "child_alive_after_second": False,
        "manifest_exists_after_second": False,
        "attempt_order": [600, 601],
    }


@pytest.mark.e2e_host
@pytest.mark.skipif(sys.platform != "win32", reason="Windows legacy capture failure test")
def test_legacy_manifest_capture_failure_is_failed_and_recoverable(tmp_path: Path) -> None:
    result = _run_legacy_stop_harness(
        tmp_path,
        LEGACY_PROCESS_TREE_HARNESS,
        scenario="pre-capture-failure",
    )
    assert result == {
        "first_failed": True,
        "root_alive_after_first": True,
        "child_alive_after_first": False,
        "retained_marker": True,
        "retained_bound": "",
        "retained_process_count": 1,
        "second_failed": False,
        "child_alive_after_second": False,
        "manifest_exists_after_second": False,
        "attempt_order": [200],
    }


@pytest.mark.e2e_host
@pytest.mark.skipif(sys.platform != "win32", reason="Windows legacy post-termination recovery test")
def test_legacy_manifest_post_termination_capture_failure_recovers_in_fixed_point(
    tmp_path: Path,
) -> None:
    result = _run_legacy_stop_harness(
        tmp_path,
        LEGACY_PROCESS_TREE_HARNESS,
        scenario="post-capture-failure",
    )
    assert result == {
        "first_failed": False,
        "root_alive_after_first": False,
        "child_alive_after_first": False,
        "retained_marker": False,
        "retained_bound": "",
        "retained_process_count": 0,
        "second_failed": False,
        "child_alive_after_second": False,
        "manifest_exists_after_second": False,
        "attempt_order": [300, 301],
    }


@pytest.mark.e2e_host
@pytest.mark.skipif(sys.platform != "win32", reason="Windows legacy process tree test")
def test_legacy_manifest_captures_and_stops_descendant_identity(tmp_path: Path) -> None:
    process_records = tmp_path / "legacy-processes"
    process_records.mkdir()
    grandchild_program = "import threading; threading.Event().wait(60)"
    child_program = (
        "import json, os, pathlib, subprocess, sys, threading; "
        f"child = subprocess.Popen([sys.executable, '-c', {grandchild_program!r}]); "
        "record = {'pid': child.pid, 'parent_pid': os.getpid()}; "
        "(pathlib.Path(sys.argv[1]) / f'{child.pid}.json').write_text(json.dumps(record), encoding='utf-8'); "
        "threading.Event().wait(60)"
    )
    root_program = (
        "import json, os, pathlib, subprocess, sys, threading; "
        "records = pathlib.Path(sys.argv[1]); program = sys.argv[2]; "
        "child = subprocess.Popen([sys.executable, '-c', program, str(records)]); "
        "(records / f'{child.pid}.json').write_text("
        "json.dumps({'pid': child.pid, 'parent_pid': os.getpid()}), encoding='utf-8'); "
        "threading.Event().wait(60)"
    )
    root_stderr_path = tmp_path / "legacy-root.stderr.log"
    with root_stderr_path.open("w", encoding="utf-8") as root_stderr:
        root = subprocess.Popen(
            [
                sys._base_executable,
                "-c",
                root_program,
                str(process_records),
                child_program,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=root_stderr,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    manifest_path = runtime_dir / "server-processes.json"
    observed_identities: dict[int, dict[str, Any]] = {}

    def matching_observed_processes() -> list[int]:
        expected_creation_ticks = {
            process_id: str(identity["start_time_utc_ticks"])
            for process_id, identity in observed_identities.items()
        }
        if not expected_creation_ticks:
            return []
        return _matching_process_identities(expected_creation_ticks)

    def observe_descendant_processes() -> list[dict[str, Any]] | None:
        records: list[dict[str, Any]] = []
        for record_path in process_records.glob("*.json"):
            record = json.loads(record_path.read_text(encoding="utf-8"))
            process_id = int(record["pid"])
            if process_id not in observed_identities:
                try:
                    observed_identities[process_id] = _windows_process_identity(process_id)
                except ProcessLookupError:
                    return None
            records.append(record)
        if len(records) != 2:
            return None
        return records

    try:
        root_identity = _wait_for_spawned_process_identity(
            root,
            stderr_path=root_stderr_path,
        )
        observed_identities[root.pid] = root_identity
        parsed_initial = _wait_for(
            observe_descendant_processes,
            timeout=5,
            description="legacy child and grandchild identities",
        )
        assert any(record["parent_pid"] != root.pid for record in parsed_initial)
        manifest_path.write_text(
            json.dumps(
                {
                    "processes": [
                        {
                            "name": "legacy-process-tree",
                            "pid": root.pid,
                            "start_time_utc_ticks": root_identity["start_time_utc_ticks"],
                            "executable": root_identity["executable"],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        stopped = _invoke_stop(runtime_dir)

        assert stopped.returncode == 0, f"{stopped.stdout}\n{stopped.stderr}"
        assert root.wait(timeout=5) != 0
        final_records = [
            json.loads(path.read_text(encoding="utf-8")) for path in process_records.glob("*.json")
        ]
        assert _wait_for(
            lambda: [] if not matching_observed_processes() else None,
            timeout=5,
            description="legacy descendant termination",
        ) == []
        assert len(final_records) == 2
        assert not manifest_path.exists()
    finally:
        if root.poll() is None:
            root.kill()
            root.wait(timeout=5)
        matching_process_ids = set(matching_observed_processes())
        cleanup_entries = [
            {
                "name": "legacy-child-fixture-cleanup",
                "pid": process_id,
                **identity,
            }
            for process_id, identity in observed_identities.items()
            if process_id in matching_process_ids
        ]
        cleanup_stop: subprocess.CompletedProcess[str] | None = None
        cleanup_stop_error: Exception | None = None
        if cleanup_entries:
            manifest_path.write_text(
                json.dumps({"processes": cleanup_entries}),
                encoding="utf-8",
            )
            try:
                cleanup_stop = _invoke_stop(runtime_dir)
            except Exception as exc:  # pragma: no cover - report cleanup failure with survivors
                cleanup_stop_error = exc

        try:
            cleanup_survivors = _wait_for(
                lambda: [] if not matching_observed_processes() else None,
                timeout=5,
                description="legacy fixture process termination",
            )
        except AssertionError as exc:
            survivors = matching_observed_processes()
            raise AssertionError(
                f"{exc}; surviving process identities: {survivors}; "
                f"cleanup stop error: {cleanup_stop_error}; cleanup stop result: {cleanup_stop}"
            ) from exc
        assert cleanup_stop_error is None, str(cleanup_stop_error)
        if cleanup_stop is not None:
            assert cleanup_stop.returncode == 0, f"{cleanup_stop.stdout}\n{cleanup_stop.stderr}"
        assert cleanup_survivors == []


@pytest.mark.e2e_host
@pytest.mark.skipif(sys.platform != "win32", reason="Windows startup recovery test")
def test_run_local_recovers_live_late_child_marker_before_startup(tmp_path: Path) -> None:
    child_pid_file = tmp_path / "late-child.pid"
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
    child_id: int | None = None
    child_identity: dict[str, Any] | None = None
    try:
        child_id = _wait_for(
            lambda: int(child_pid_file.read_text(encoding="ascii")) if child_pid_file.exists() else None,
            timeout=5,
            description="late child PID",
        )
        root_identity = _windows_process_identity(root.pid)
        child_identity = _windows_process_identity(child_id)
        snapshot = _windows_process_snapshot(process_ids=[child_id])
        assert int(snapshot[child_id]["ParentProcessId"]) == root.pid

        root.terminate()
        root.wait(timeout=5)
        termination_bound = max(
            _datetime_to_dotnet_ticks(datetime.now(UTC)),
            int(child_identity["start_time_utc_ticks"]),
        )
        manifest_path.write_text(
            json.dumps(
                {
                    "processes": [
                        {
                            "name": "legacy-startup-recovery",
                            "pid": root.pid,
                            **root_identity,
                            "job_managed": False,
                            "legacy_descendant_scan_incomplete": True,
                            "legacy_termination_utc_ticks": str(termination_bound),
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

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

        assert started.returncode != 0
        assert "Environment file does not exist" in started.stderr
        assert _wait_for(
            lambda: []
            if not _matching_process_identities(
                {child_id: str(child_identity["start_time_utc_ticks"])}
            )
            else None,
            timeout=5,
            description="startup recovery late child termination",
        ) == []
        assert not manifest_path.exists()
    finally:
        if root.poll() is None:
            root.kill()
            root.wait(timeout=5)
        if child_id is not None and child_identity is not None:
            if _matching_process_identities(
                {child_id: str(child_identity["start_time_utc_ticks"])}
            ):
                manifest_path.write_text(
                    json.dumps(
                        {
                            "processes": [
                                {
                                    "name": "legacy-startup-recovery-fixture-cleanup",
                                    "pid": child_id,
                                    **child_identity,
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                _invoke_stop(runtime_dir)


@pytest.mark.e2e_host
@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object startup cleanup test")
def test_windows_job_supervisor_start_failure_releases_started_processes(tmp_path: Path) -> None:
    director_port, worker_base_port = _reserve_process_ports()
    runtime_dir = tmp_path / "runtime"
    env_file = tmp_path / "startup-failure.env"
    env_file.write_text(
        "\n".join(_cluster_environment(worker_base_port, redis_url=None, redis_prefix=None)) + "\n",
        encoding="utf-8",
    )
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", worker_base_port))
    manifest_path = runtime_dir / "server-processes.json"
    recovery_manifest: dict[str, Any] | None = None
    recovery_identities: dict[int, str] = {}
    try:
        try:
            started = subprocess.run(
                [
                    "pwsh",
                    "-NoProfile",
                    "-File",
                    str(RUN_LOCAL),
                    "-WorkerCount",
                    "1",
                    "-RuntimeDirectory",
                    str(runtime_dir),
                    "-EnvironmentFile",
                    str(env_file),
                    "-DirectorPort",
                    str(director_port),
                    "-WorkerBasePort",
                    str(worker_base_port),
                    "-UdpBasePort",
                    str(worker_base_port + 1),
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
                env=_clean_process_environment(),
            )
        finally:
            blocker.close()

        recovery_manifest = _read_manifest_if_present(manifest_path)
        if recovery_manifest is not None:
            recovery_identities = _owned_process_identities(recovery_manifest)
        assert started.returncode != 0, f"{started.stdout}\n{started.stderr}"
        assert recovery_manifest is None, f"{started.stdout}\n{started.stderr}"
        assert _wait_for(
            lambda: True if _ports_can_bind([director_port, worker_base_port]) else None,
            timeout=5,
            description="ports after startup failure cleanup",
        )
    finally:
        blocker.close()
        current_manifest = _read_manifest_if_present(manifest_path) or recovery_manifest
        if current_manifest is not None:
            stopped = _invoke_stop(runtime_dir)
            if stopped.returncode != 0:
                _force_close_verified_supervisors(current_manifest)
                _invoke_stop(runtime_dir)
        if recovery_identities:
            _wait_for(
                lambda: [] if not _matching_process_identities(recovery_identities) else None,
                timeout=5,
                description="startup failure fixture cleanup",
            )


@pytest.mark.e2e_host
@pytest.mark.skipif(sys.platform != "win32", reason="run-local.ps1 uses Windows Job Objects")
def test_two_worker_processes_spill_over_and_drain_with_redis(tmp_path: Path) -> None:
    redis_url = os.getenv("VOICE_TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("VOICE_TEST_REDIS_URL is not configured")

    redis_client = Redis.from_url(redis_url, socket_connect_timeout=1, socket_timeout=1)
    try:
        redis_client.ping()
    except RedisError:
        pytest.skip("VOICE_TEST_REDIS_URL is unreachable")

    redis_prefix = f"voice-process-smoke-{uuid.uuid4().hex}"
    try:
        with _running_cluster(tmp_path, redis_url=redis_url, redis_prefix=redis_prefix) as manifest:
            director_port = int(manifest["director_port"])
            worker_processes = {
                process["environment"]["VOICE_WORKER_ID"]: process
                for process in manifest["processes"]
                if process["name"].startswith("realtime-worker-")
            }
            assert set(worker_processes) == {"worker-local-1", "worker-local-2"}

            with httpx.Client(base_url=f"http://127.0.0.1:{director_port}", timeout=2) as director:
                workers = _wait_for(
                    lambda: _workers(director),
                    timeout=5,
                    description="two worker heartbeats",
                )
                assert {worker["max_sessions"] for worker in workers.values()} == {5}
                assert len({worker["public_wss_url"] for worker in workers.values()}) == 2

                first_expiries = {worker_id: worker["heartbeat_expires_at"] for worker_id, worker in workers.items()}
                workers = _wait_for(
                    lambda: (
                        current
                        if (current := _workers(director))
                        and all(
                            current[worker_id]["heartbeat_expires_at"] > expiry
                            for worker_id, expiry in first_expiries.items()
                        )
                        else None
                    ),
                    timeout=4,
                    description="independent worker heartbeat renewal",
                )

                routes: list[dict[str, Any]] = []
                for index in range(7):
                    response = director.post(
                        "/v1/session/bootstrap",
                        headers={"Authorization": f"Bearer {BOOTSTRAP_TOKEN}"},
                        json={"tenant_id": "scale-smoke", "device_id": f"device-{index}"},
                    )
                    assert response.status_code == 200, response.text
                    routes.append(response.json())
                assert [route["worker_id"] for route in routes] == ["worker-local-1"] * 5 + ["worker-local-2"] * 2

                consumed = director.post(
                    "/internal/v1/grants/consume",
                    headers={"X-Internal-Token": INTERNAL_TOKEN},
                    json={
                        "token": routes[0]["connect_grant"],
                        "worker_id": routes[0]["worker_id"],
                        "device_id": "device-0",
                    },
                )
                assert consumed.status_code == 200, consumed.text

                released = director.post(
                    "/internal/v1/workers/heartbeat",
                    headers={"X-Internal-Token": INTERNAL_TOKEN},
                    json={
                        "worker_id": "worker-local-1",
                        "public_wss_url": workers["worker-local-1"]["bindings"][0]["public_wss_url"],
                        "active_sessions": 0,
                        "max_sessions": 5,
                        "draining": False,
                        "healthy": True,
                        "profiles": ["wss-opus-v3"],
                        "bindings": workers["worker-local-1"]["bindings"],
                        "released_leases": [
                            {
                                "tenant_id": "scale-smoke",
                                "device_id": f"device-{index}",
                                "session_epoch": routes[index]["session_epoch"],
                                "fencing_token": routes[index]["fencing_token"],
                            }
                            for index in range(5)
                        ],
                    },
                )
                assert released.status_code == 200, released.text

                worker_one_port = int(worker_processes["worker-local-1"]["environment"]["VOICE_WORKER_BIND_PORT"])
                drained = httpx.post(
                    f"http://127.0.0.1:{worker_one_port}/internal/v1/drain",
                    headers={"X-Internal-Token": INTERNAL_TOKEN},
                    timeout=2,
                )
                assert drained.status_code == 200, drained.text
                workers = _wait_for(
                    lambda: (
                        current
                        if (current := _workers(director)) and current["worker-local-1"]["draining"] is True
                        else None
                    ),
                    timeout=3,
                    description="worker drain heartbeat",
                )

                fresh = director.post(
                    "/v1/session/bootstrap",
                    headers={"Authorization": f"Bearer {BOOTSTRAP_TOKEN}"},
                    json={"tenant_id": "scale-smoke", "device_id": "device-fresh"},
                )
                assert fresh.status_code == 200, fresh.text
                assert fresh.json()["worker_id"] == "worker-local-2"
    finally:
        keys = list(redis_client.scan_iter(match=f"{redis_prefix}:*"))
        if keys:
            redis_client.delete(*keys)
        redis_client.close()
