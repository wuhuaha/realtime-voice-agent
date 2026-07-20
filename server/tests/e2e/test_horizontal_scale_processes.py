from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from redis import Redis
from redis.exceptions import RedisError

SERVER_ROOT = Path(__file__).resolve().parents[2]
RUN_LOCAL = SERVER_ROOT / "scripts" / "run-local.ps1"
JOB_SUPERVISOR = SERVER_ROOT / "scripts" / "windows_job_supervisor.py"
INTERNAL_TOKEN = "validator-horizontal-scale-internal-token"
BOOTSTRAP_TOKEN = "validator-horizontal-scale-bootstrap-token"


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


def _workers(client: httpx.Client) -> dict[str, dict[str, Any]] | None:
    response = client.get("/internal/v1/workers", headers={"X-Internal-Token": INTERNAL_TOKEN})
    if response.status_code != 200:
        return None
    workers = response.json()
    if len(workers) != 2:
        return None
    return {worker["worker_id"]: worker for worker in workers}


def _windows_process_snapshot() -> dict[int, dict[str, Any]]:
    result = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-Command",
            (
                "@(Get-CimInstance Win32_Process | "
                "Select-Object ProcessId,ParentProcessId,CreationDate) | ConvertTo-Json -Compress"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    rows = json.loads(result.stdout)
    return {int(row["ProcessId"]): row for row in rows}


def _owned_process_identities(manifest: dict[str, Any]) -> dict[int, str]:
    snapshot = _windows_process_snapshot()
    owned: dict[int, str] = {}
    pending = [int(entry["supervisor_pid"]) for entry in manifest["processes"]]
    while pending:
        process_id = pending.pop()
        row = snapshot.get(process_id)
        if row is None or process_id in owned:
            continue
        owned[process_id] = str(row["CreationDate"])
        pending.extend(child_id for child_id, child in snapshot.items() if int(child["ParentProcessId"]) == process_id)
    return owned


def _matching_process_identities(identities: dict[int, str]) -> list[int]:
    snapshot = _windows_process_snapshot()
    return [
        process_id
        for process_id, creation_date in identities.items()
        if (row := snapshot.get(process_id)) is not None and str(row["CreationDate"]) == creation_date
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
    result = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-Command",
            (
                f"$process = Get-Process -Id {process_id}; "
                "[pscustomobject]@{ "
                "executable = $process.Path; "
                "start_time_utc = $process.StartTime.ToUniversalTime().ToString('O'); "
                "start_time_utc_ticks = [string]$process.StartTime.ToUniversalTime().Ticks "
                "} | ConvertTo-Json -Compress"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=5,
    )
    return json.loads(result.stdout)


def _assert_manifest_process_identities(manifest: dict[str, Any]) -> None:
    snapshot = _windows_process_snapshot()
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
        f"VOICE_WORKER_PUBLIC_WS_URL=ws://127.0.0.1:{worker_base_port}/v1/xiaozhi",
        "VOICE_WORKER_BIND_HOST=127.0.0.1",
        "VOICE_HEARTBEAT_INTERVAL_SECONDS=1",
        "VOICE_XIAOZHI_UDP_ENABLED=false",
        "VOICE_XIAOZHI_TRANSPORT_POLICY=force_wss",
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
        process_survivors = _wait_for(
            lambda: [] if not _matching_process_identities(owned_identities) else None,
            timeout=5,
            description="Job Object process tree release",
        )
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

        assert stopped.returncode == 0, f"{stopped.stdout}\n{stopped.stderr}"
        assert process.wait(timeout=5) != 0
        assert not manifest_path.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.e2e_host
@pytest.mark.skipif(sys.platform != "win32", reason="Windows legacy process tree test")
def test_legacy_manifest_captures_and_stops_descendant_identity(tmp_path: Path) -> None:
    child_pid_file = tmp_path / "child.pid"
    root = subprocess.Popen(
        [
            sys._base_executable,
            "-c",
            (
                "import pathlib, subprocess, sys, time; "
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                "pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding='ascii'); "
                "time.sleep(60)"
            ),
            str(child_pid_file),
        ],
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
    child_creation = ""
    try:
        child_id = _wait_for(
            lambda: int(child_pid_file.read_text(encoding="ascii")) if child_pid_file.exists() else None,
            timeout=5,
            description="legacy child PID",
        )
        root_identity = _windows_process_identity(root.pid)
        child_identity = _windows_process_identity(child_id)
        child_creation = str(_windows_process_snapshot()[child_id]["CreationDate"])
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
        assert _wait_for(
            lambda: [] if not _matching_process_identities({child_id: child_creation}) else None,
            timeout=5,
            description="legacy descendant termination",
        ) == []
        assert not manifest_path.exists()
    finally:
        if root.poll() is None:
            root.kill()
            root.wait(timeout=5)
        if child_id is not None and child_identity is None:
            try:
                child_identity = _windows_process_identity(child_id)
            except subprocess.SubprocessError:
                pass
        if child_id is not None and child_identity is not None:
            manifest_path.write_text(
                json.dumps(
                    {
                        "processes": [
                            {
                                "name": "legacy-child-fixture-cleanup",
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
                        "public_wss_url": workers["worker-local-1"]["public_wss_url"],
                        "active_sessions": 0,
                        "max_sessions": 5,
                        "draining": False,
                        "healthy": True,
                        "profiles": ["wss-opus-v1"],
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
