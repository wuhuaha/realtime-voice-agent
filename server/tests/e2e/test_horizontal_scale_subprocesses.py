from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from redis import Redis
from redis.exceptions import RedisError
from voice_testkit.subprocess_cluster import BOOTSTRAP_TOKEN, INTERNAL_TOKEN, running_process_cluster


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


def _workers(client: httpx.Client) -> dict[str, dict[str, Any]] | None:
    response = client.get("/internal/v1/workers", headers={"X-Internal-Token": INTERNAL_TOKEN})
    if response.status_code != 200:
        return None
    workers = response.json()
    if len(workers) != 2:
        return None
    return {worker["worker_id"]: worker for worker in workers}


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
        with running_process_cluster(
            tmp_path,
            worker_count=2,
            redis_url=redis_url,
            redis_prefix=redis_prefix,
        ) as cluster:
            worker_ports = {worker.worker_id: worker.http_port for worker in cluster.workers}
            with httpx.Client(base_url=cluster.director_url, timeout=2) as director:
                workers = _wait_for(lambda: _workers(director), timeout=5, description="two worker heartbeats")
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
                        "profiles": ["wss-opus/1"],
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

                drained = httpx.post(
                    f"http://127.0.0.1:{worker_ports['worker-local-1']}/internal/v1/drain",
                    headers={"X-Internal-Token": INTERNAL_TOKEN},
                    timeout=2,
                )
                assert drained.status_code == 200, drained.text
                _wait_for(
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
