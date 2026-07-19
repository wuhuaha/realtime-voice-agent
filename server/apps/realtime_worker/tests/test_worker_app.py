from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient
from realtime_worker import app as app_module
from realtime_worker.app import DirectorGrantConsumer, WorkerHeartbeatLoop, create_app
from realtime_worker.auth import AuthContext
from realtime_worker.bindings.xiaozhi import SharedSessionAdmission, XiaozhiSessionRegistry
from realtime_worker.config import Settings
from voice_contracts import LeaseRenewal
from voice_testkit import MutableClock


def settings(**changes: object) -> Settings:
    values: dict[str, object] = {
        "worker_id": "worker-a",
        "lab_token": "lab-test-token",
        "grant_signing_key": "validator-grant-signing-key-with-32-bytes",
        "internal_token": "validator-internal-token",
        "runner": "deterministic",
        "xiaozhi_udp_enabled": False,
        "director_url": "",
        "heartbeat_enabled": False,
    }
    values.update(changes)
    return Settings(_env_file=None, **values)


def test_worker_exposes_xiaozhi_and_health_without_direct_routes() -> None:
    app = create_app(settings())
    paths = {route.path for route in app.routes}
    assert "/v1/xiaozhi" in paths
    assert "/v1/direct" not in paths
    assert "/v1/device/bootstrap" not in paths

    with TestClient(app) as client:
        ready = client.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json()["max_sessions"] == 5
        assert ready.json()["provider_network_checked"] is True
        assert ready.json()["provider_network_ready"] is True


def test_worker_drain_rejects_readiness_and_requires_internal_credential() -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        assert client.post("/internal/v1/drain").status_code == 401
        drained = client.post("/internal/v1/drain", headers={"X-Internal-Token": "validator-internal-token"})
        assert drained.status_code == 200
        assert client.get("/health/ready").status_code == 503


async def test_worker_heartbeat_reports_capacity_and_applies_director_drain() -> None:
    requests: list[dict[str, object]] = []
    revoked: list[set[str]] = []

    class FakeRegistry:
        def active_lease_renewals(self) -> tuple[LeaseRenewal, ...]:
            return (
                LeaseRenewal(
                    tenant_id="tenant-1",
                    device_id="device-1",
                    session_epoch="epoch-1",
                    fencing_token=2,
                ),
            )

        async def revoke_session_epochs(self, epochs: set[str]) -> None:
            revoked.append(epochs)

        async def revoke_expired_leases(self, now: float) -> None:
            assert now > 0

        def extend_lease_deadlines(self, expires_at: float, rejected: set[str]) -> None:
            assert expires_at == 153.0
            assert rejected == {"epoch-1"}

        def pending_lease_releases(self) -> tuple[LeaseRenewal, ...]:
            return ()

        def acknowledge_lease_releases(self, releases: tuple[LeaseRenewal, ...]) -> None:
            assert releases == ()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.read()))
        return httpx.Response(
            200,
            json={
                "accepted": True,
                "draining": True,
                "heartbeat_expires_at": 123.0,
                "lease_expires_at": 153.0,
                "rejected_session_epochs": ["epoch-1"],
            },
        )

    worker_settings = settings(
        director_url="http://director.test",
        heartbeat_enabled=True,
        worker_public_ws_url="ws://worker-a.test/v1/xiaozhi",
    )
    admission = SharedSessionAdmission(worker_settings.max_sessions)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        heartbeat = WorkerHeartbeatLoop(worker_settings, admission, FakeRegistry(), client=client)  # type: ignore[arg-type]
        await heartbeat.send_once()

    assert requests[0]["worker_id"] == "worker-a"
    assert requests[0]["max_sessions"] == 5
    assert requests[0]["active_sessions"] == 0
    assert requests[0]["healthy"] is True
    assert requests[0]["active_leases"] == [
        {
            "tenant_id": "tenant-1",
            "device_id": "device-1",
            "session_epoch": "epoch-1",
            "fencing_token": 2,
        }
    ]
    assert admission.draining is True
    assert revoked == [{"epoch-1"}]


@pytest.mark.asyncio
async def test_heartbeat_enforces_local_deadline_before_unavailable_director() -> None:
    checked: list[float] = []

    class FakeRegistry:
        async def revoke_expired_leases(self, now: float) -> None:
            checked.append(now)

        def pending_lease_releases(self) -> tuple[LeaseRenewal, ...]:
            return ()

        def active_lease_renewals(self) -> tuple[LeaseRenewal, ...]:
            return ()

    def unavailable(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("director unavailable")

    clock = MutableClock(200.0)
    worker_settings = settings(director_url="http://director.test", heartbeat_enabled=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
        heartbeat = WorkerHeartbeatLoop(
            worker_settings,
            SharedSessionAdmission(5),
            FakeRegistry(),  # type: ignore[arg-type]
            client=client,
            clock=clock,
        )
        with pytest.raises(httpx.ConnectError):
            await heartbeat.send_once()
    assert checked == [200.0]


@pytest.mark.asyncio
async def test_registry_closes_connection_after_local_lease_deadline() -> None:
    registry = XiaozhiSessionRegistry(settings(), SharedSessionAdmission(5))
    closed: list[tuple[int, str]] = []

    class FakeConnection:
        auth_context = AuthContext(
            tenant_id="tenant-1",
            device_id="device-1",
            session_epoch="epoch-1",
            fencing_token=1,
            expires_at=100.0,
        )

        async def close(self, *, code: int, reason: str) -> None:
            closed.append((code, reason))

    registry._connections[("tenant-1", "device-1")] = FakeConnection()  # type: ignore[assignment]  # noqa: SLF001
    registry._lease_deadlines["epoch-1"] = 100.0  # noqa: SLF001
    await registry.revoke_expired_leases(100.0)
    assert closed == [(1008, "stale_route_lease")]


@pytest.mark.asyncio
async def test_grant_consumer_fails_closed_when_director_store_is_unavailable() -> None:
    def unavailable(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("coordination unavailable")

    worker_settings = settings(director_url="http://director.test")
    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
        consumer = DirectorGrantConsumer(worker_settings, client=client)
        with pytest.raises(httpx.ConnectError):
            await consumer.consume("signed-token", device_id="device-1")


def test_live_provider_worker_is_not_ready_when_network_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unavailable(_url: str, *, timeout: float) -> bool:
        assert timeout > 0
        return False

    monkeypatch.setattr(app_module, "_probe_endpoint", unavailable)
    live_settings = settings(runner="livekit", deepseek_api_key="validator-provider-key")
    app = create_app(live_settings)
    with TestClient(app) as client:
        ready = client.get("/health/ready")
        assert ready.status_code == 503
        assert ready.json()["provider_network_checked"] is True
        assert ready.json()["provider_network_ready"] is False
