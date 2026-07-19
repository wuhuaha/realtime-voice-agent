from __future__ import annotations

import json
import os
import socket
import subprocess
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


def _pid_exists(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        synchronize = 0x00100000
        wait_timeout = 0x00000102
        handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return False
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == wait_timeout
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


@contextmanager
def _running_cluster(
    tmp_path: Path,
    *,
    redis_url: str,
    redis_prefix: str,
) -> Iterator[dict[str, Any]]:
    director_port, worker_base_port = _reserve_process_ports()
    runtime_dir = tmp_path / "runtime"
    env_file = tmp_path / "horizontal-scale.env"
    env_file.write_text(
        "\n".join(
            (
                "VOICE_ENV=development",
                "VOICE_ALLOW_SHARED_BOOTSTRAP_AUTH=true",
                "VOICE_ALLOW_LAB_AUTH=true",
                "VOICE_DIRECTOR_BIND_HOST=127.0.0.1",
                "VOICE_COORDINATION_BACKEND=redis",
                f"VOICE_REDIS_URL={redis_url}",
                f"VOICE_COORDINATION_PREFIX={redis_prefix}",
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
            )
        )
        + "\n",
        encoding="utf-8",
    )
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
    assert started.returncode == 0, f"{launcher_stdout.read_text()}\n{launcher_stderr.read_text()}"
    manifest_path = runtime_dir / "server-processes.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    pids = [int(process["pid"]) for process in manifest["processes"]]
    assert len(pids) == 3
    assert len(set(pids)) == 3
    try:
        yield manifest
    finally:
        stop_started = time.monotonic()
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
            timeout=20,
            env=_clean_process_environment(),
        )
        stop_elapsed = time.monotonic() - stop_started
        assert stopped.returncode == 0, f"{stopped.stdout}\n{stopped.stderr}"
        assert stop_elapsed < 20
        assert not manifest_path.exists()
        assert not [pid for pid in pids if _pid_exists(pid)]


@pytest.mark.e2e_host
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
