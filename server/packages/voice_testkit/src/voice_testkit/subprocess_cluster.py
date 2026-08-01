from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

INTERNAL_TOKEN = "validator-host-e2e-internal-token"
BOOTSTRAP_TOKEN = "validator-host-e2e-bootstrap-token"
GRANT_SIGNING_KEY = "validator-host-e2e-grant-signing-key"
LAB_TOKEN = "validator-host-e2e-lab-token"


@dataclass(frozen=True)
class WorkerProcess:
    worker_id: str
    http_port: int
    udp_port: int


@dataclass(frozen=True)
class ProcessCluster:
    director_url: str
    director_port: int
    workers: tuple[WorkerProcess, ...]


def _reserve_port(sock_type: int, used: set[int]) -> int:
    while True:
        with socket.socket(socket.AF_INET, sock_type) as reserved:
            reserved.bind(("127.0.0.1", 0))
            port = int(reserved.getsockname()[1])
        if port not in used:
            used.add(port)
            return port


def _clean_environment() -> dict[str, str]:
    return {name: value for name, value in os.environ.items() if not name.upper().startswith("VOICE_")}


def _wait_for(probe: Any, *, timeout: float, description: str) -> Any:
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


def _ready(url: str) -> bool | None:
    response = httpx.get(f"{url}/health/ready", timeout=1)
    return True if response.status_code == 200 else None


def _registered_workers(director_url: str, expected: int, internal_token: str) -> bool | None:
    response = httpx.get(
        f"{director_url}/internal/v1/workers",
        headers={"X-Internal-Token": internal_token},
        timeout=1,
    )
    return True if response.status_code == 200 and len(response.json()) == expected else None


def _start_process(
    module: str,
    *,
    python_executable: Path,
    environment: dict[str, str],
    log_path: Path,
) -> tuple[subprocess.Popen[bytes], Any]:
    log = log_path.open("wb")
    kwargs: dict[str, Any] = {"start_new_session": True} if os.name != "nt" else {"creationflags": 0x00000200}
    process = subprocess.Popen(
        [str(python_executable), "-m", module],
        env=environment,
        stdout=log,
        stderr=subprocess.STDOUT,
        **kwargs,
    )
    return process, log


def _assert_running(process: subprocess.Popen[bytes], log_path: Path, name: str) -> None:
    returncode = process.poll()
    if returncode is None:
        return
    detail = log_path.read_text(encoding="utf-8", errors="replace")[-4_000:]
    raise AssertionError(f"{name} exited before readiness (returncode={returncode})\n{detail}")


def _stop_process(process: subprocess.Popen[bytes], *, timeout: float = 5) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        process.terminate()
    else:
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=timeout)


def _ports_released(cluster: ProcessCluster) -> bool:
    sockets: list[socket.socket] = []
    try:
        for port in [cluster.director_port, *(worker.http_port for worker in cluster.workers)]:
            candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            candidate.bind(("127.0.0.1", port))
            sockets.append(candidate)
        for worker in cluster.workers:
            candidate = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            candidate.bind(("127.0.0.1", worker.udp_port))
            sockets.append(candidate)
    except OSError:
        return False
    finally:
        for candidate in sockets:
            candidate.close()
    return True


@contextmanager
def running_process_cluster(
    tmp_path: Path,
    *,
    worker_count: int,
    udp_enabled: bool = False,
    redis_url: str | None = None,
    redis_prefix: str | None = None,
    python_executable: Path | None = None,
    internal_token: str = INTERNAL_TOKEN,
    bootstrap_token: str = BOOTSTRAP_TOKEN,
    grant_signing_key: str = GRANT_SIGNING_KEY,
    lab_token: str = LAB_TOKEN,
) -> Iterator[ProcessCluster]:
    if worker_count < 1:
        raise ValueError("worker_count must be positive")
    if redis_url is not None and not redis_prefix:
        raise ValueError("redis_prefix is required with redis_url")

    used_ports: set[int] = set()
    python = python_executable or Path(sys.executable)
    director_port = _reserve_port(socket.SOCK_STREAM, used_ports)
    workers = tuple(
        WorkerProcess(
            worker_id=f"worker-local-{index + 1}",
            http_port=_reserve_port(socket.SOCK_STREAM, used_ports),
            udp_port=_reserve_port(socket.SOCK_DGRAM, used_ports),
        )
        for index in range(worker_count)
    )
    cluster = ProcessCluster(f"http://127.0.0.1:{director_port}", director_port, workers)
    common = {
        **_clean_environment(),
        "VOICE_ENV": "development",
        "VOICE_ALLOW_SHARED_BOOTSTRAP_AUTH": "true",
        "VOICE_ALLOW_LAB_AUTH": "true",
        "VOICE_INTERNAL_TOKEN": internal_token,
        "VOICE_GRANT_SIGNING_KEY": grant_signing_key,
        "VOICE_DEVICE_BOOTSTRAP_TOKEN": bootstrap_token,
        "VOICE_LAB_TOKEN": lab_token,
        "VOICE_HEARTBEAT_INTERVAL_SECONDS": "1",
        "VOICE_ROUTE_LEASE_TTL_SECONDS": "5",
        "VOICE_RUNNER": "deterministic",
        # Host E2E validates protocol/lifecycle behavior; latency budgets are measured separately.
        "VOICE_RVA_UPLINK_MAX_AGE_SECONDS": "2.0",
    }
    director_env = {
        **common,
        "VOICE_DIRECTOR_BIND_HOST": "127.0.0.1",
        "VOICE_DIRECTOR_BIND_PORT": str(director_port),
        "VOICE_COORDINATION_BACKEND": "redis" if redis_url else "memory",
    }
    if redis_url:
        director_env["VOICE_REDIS_URL"] = redis_url
        director_env["VOICE_COORDINATION_PREFIX"] = str(redis_prefix)

    processes: list[tuple[subprocess.Popen[bytes], Any]] = []
    try:
        processes.append(
            _start_process(
                "session_director",
                python_executable=python,
                environment=director_env,
                log_path=tmp_path / "director.log",
            )
        )
        director_process, _ = processes[0]
        _wait_for(
            lambda: (
                _assert_running(director_process, tmp_path / "director.log", "Director"),
                _ready(cluster.director_url),
            )[1],
            timeout=15,
            description="Director readiness",
        )
        for worker in workers:
            worker_env = {
                **common,
                "VOICE_WORKER_ID": worker.worker_id,
                "VOICE_WORKER_BIND_HOST": "127.0.0.1",
                "VOICE_WORKER_BIND_PORT": str(worker.http_port),
                "VOICE_UDP_BIND_HOST": "127.0.0.1",
                "VOICE_UDP_BIND_PORT": str(worker.udp_port),
                "VOICE_UDP_ADVERTISE_HOST": "127.0.0.1",
                "VOICE_UDP_ADVERTISE_PORT": str(worker.udp_port),
                "VOICE_RVA_UDP_ENABLED": str(udp_enabled).lower(),
                "VOICE_RVA_PUBLIC_WS_URL": f"ws://127.0.0.1:{worker.http_port}/v2/voice",
                "VOICE_DIRECTOR_URL": cluster.director_url,
            }
            process_and_log = _start_process(
                "realtime_worker",
                python_executable=python,
                environment=worker_env,
                log_path=tmp_path / f"{worker.worker_id}.log",
            )
            processes.append(process_and_log)
            worker_process, _ = process_and_log
            worker_log = tmp_path / f"{worker.worker_id}.log"
            _wait_for(
                lambda worker_process=worker_process, worker_log=worker_log, worker=worker: (
                    _assert_running(worker_process, worker_log, worker.worker_id),
                    _ready(f"http://127.0.0.1:{worker.http_port}"),
                )[1],
                timeout=15,
                description=f"{worker.worker_id} readiness",
            )
        _wait_for(
            lambda: _registered_workers(cluster.director_url, worker_count, internal_token),
            timeout=10,
            description="worker registration",
        )
        yield cluster
    finally:
        for process, _ in reversed(processes):
            _stop_process(process)
        for _, log in processes:
            log.close()
        _wait_for(lambda: True if _ports_released(cluster) else None, timeout=5, description="process port release")
