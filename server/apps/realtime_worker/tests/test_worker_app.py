from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from realtime_worker import app as app_module
from realtime_worker.admission import SharedSessionAdmission
from realtime_worker.app import DirectorGrantConsumer, RvaSessionRegistry, WorkerHeartbeatLoop, create_app
from realtime_worker.auth import AuthContext
from realtime_worker.config import Settings
from realtime_worker.lifecycle import detached_shutdown_task_count
from voice_contracts import LeaseRenewal
from voice_testkit import MutableClock


def settings(**changes: object) -> Settings:
    values: dict[str, object] = {
        "worker_id": "worker-a",
        "lab_token": "lab-test-token",
        "grant_signing_key": "validator-grant-signing-key-with-32-bytes",
        "internal_token": "validator-internal-token",
        "runner": "deterministic",
        "director_url": "",
        "heartbeat_enabled": False,
    }
    values.update(changes)
    return Settings(_env_file=None, **values)


def test_shared_udp_gateway_uses_neutral_environment_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOICE_UDP_BIND_HOST", "127.0.0.2")
    monkeypatch.setenv("VOICE_UDP_BIND_PORT", "18092")
    monkeypatch.setenv("VOICE_UDP_ADVERTISE_HOST", "voice.example.test")
    monkeypatch.setenv("VOICE_UDP_ADVERTISE_PORT", "28092")

    value = Settings(_env_file=None)

    assert value.udp_bind_host == "127.0.0.2"
    assert value.udp_bind_port == 18092
    assert value.udp_advertise_host == "voice.example.test"
    assert value.udp_advertise_port == 28092


def test_production_requires_provider_readiness_gate() -> None:
    value = settings(
        environment="production",
        allow_lab_auth=False,
        director_url="https://director.example.test",
        heartbeat_enabled=True,
        rva_public_ws_url="wss://voice.example.test/v2/voice",
    )

    with pytest.raises(ValueError, match="VOICE_PROVIDER_READINESS_REQUIRED"):
        value.validate_runtime()


def test_worker_exposes_only_rva_and_health_by_default() -> None:
    app = create_app(settings())
    paths = {route.path for route in app.routes}
    assert "/v2/voice" in paths
    assert "/v1/direct" not in paths
    assert "/v1/device/bootstrap" not in paths

    with TestClient(app) as client:
        ready = client.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json()["max_sessions"] == 5
        assert ready.json()["provider_network_checked"] is True
        assert ready.json()["provider_network_ready"] is True
        assert ready.json()["coordination_ready"] is True
        assert ready.json()["rva_enabled"] is True


def test_worker_drain_rejects_readiness_and_requires_internal_credential() -> None:
    app = create_app(settings())
    with TestClient(app) as client:
        assert client.post("/internal/v1/drain").status_code == 401
        drained = client.post("/internal/v1/drain", headers={"X-Internal-Token": "validator-internal-token"})
        assert drained.status_code == 200
        assert client.get("/health/ready").status_code == 503


def test_worker_shutdown_reports_draining_then_releases_before_closing_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, object]] = []
    releases: list[LeaseRenewal] = []

    class FakeRegistry:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def close(self) -> None:
            events.append(("registry_close", None))
            releases.append(
                LeaseRenewal(
                    tenant_id="tenant-1",
                    device_id="device-1",
                    session_epoch="epoch-1",
                    fencing_token=2,
                )
            )

        async def revoke_expired_leases(self, _now: float) -> None:
            return None

        async def revoke_session_epochs(self, _session_epochs: set[str]) -> None:
            return None

        def extend_lease_deadlines(self, _expires_at: float, _rejected_epochs: set[str]) -> None:
            return None

        def active_lease_renewals(self) -> tuple[LeaseRenewal, ...]:
            return ()

        def pending_lease_releases(self) -> tuple[LeaseRenewal, ...]:
            return tuple(releases)

        def acknowledge_lease_releases(self, acknowledged: tuple[LeaseRenewal, ...]) -> None:
            for release in acknowledged:
                releases.remove(release)

    class FakeHeartbeat:
        last_success = True

        def __init__(
            self,
            _settings: Settings,
            admission: SharedSessionAdmission,
            registry: FakeRegistry,
            *args: object,
            **kwargs: object,
        ) -> None:
            self._admission = admission
            self._registry = registry

        def start(self) -> None:
            return None

        async def send_once(self) -> None:
            pending = self._registry.pending_lease_releases()
            events.append(("heartbeat", (self._admission.draining, pending)))
            self._registry.acknowledge_lease_releases(pending)

        async def close(self) -> None:
            events.append(("heartbeat_close", None))

    class FakeGrantConsumer:
        async def close(self) -> None:
            events.append(("grant_consumer_close", None))

    monkeypatch.setattr(app_module, "RvaSessionRegistry", FakeRegistry)
    monkeypatch.setattr(app_module, "WorkerHeartbeatLoop", FakeHeartbeat)
    app = create_app(
        settings(director_url="http://director.test", heartbeat_enabled=True),
        grant_consumer=FakeGrantConsumer(),  # type: ignore[arg-type]
    )

    with TestClient(app):
        pass

    assert events == [
        ("heartbeat", (True, ())),
        ("registry_close", None),
        (
            "heartbeat",
            (
                True,
                (
                    LeaseRenewal(
                        tenant_id="tenant-1",
                        device_id="device-1",
                        session_epoch="epoch-1",
                        fencing_token=2,
                    ),
                ),
            ),
        ),
        ("heartbeat_close", None),
        ("grant_consumer_close", None),
    ]


def test_worker_shutdown_cancels_hung_registry_close_at_configured_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    releases: list[LeaseRenewal] = []
    release = LeaseRenewal(
        tenant_id="tenant-1",
        device_id="device-1",
        session_epoch="epoch-1",
        fencing_token=2,
    )

    class HungRegistry:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def close(self) -> None:
            events.append("registry_close_started")
            releases.append(release)
            child = asyncio.create_task(asyncio.Event().wait(), name="shielded-registry-child")
            try:
                await asyncio.shield(child)
            finally:
                child.cancel()
                await asyncio.gather(child, return_exceptions=True)
                events.append("registry_child_reaped")

        async def revoke_expired_leases(self, _now: float) -> None:
            return None

        async def revoke_session_epochs(self, _session_epochs: set[str]) -> None:
            return None

        def extend_lease_deadlines(self, _expires_at: float, _rejected_epochs: set[str]) -> None:
            return None

        def active_lease_renewals(self) -> tuple[LeaseRenewal, ...]:
            return ()

        def pending_lease_releases(self) -> tuple[LeaseRenewal, ...]:
            return tuple(releases)

        def acknowledge_lease_releases(self, _acknowledged: tuple[LeaseRenewal, ...]) -> None:
            releases.clear()

    class FastHeartbeat:
        last_success = True

        def __init__(
            self,
            _settings: object,
            _admission: object,
            registry: HungRegistry,
            *_args: object,
            **_kwargs: object,
        ) -> None:
            self._registry = registry

        def start(self) -> None:
            return None

        async def send_once(self) -> None:
            pending = self._registry.pending_lease_releases()
            if pending:
                events.append("final_release_sent")
                self._registry.acknowledge_lease_releases(pending)

        async def close(self) -> None:
            events.append("heartbeat_close")

    class FakeGrantConsumer:
        async def close(self) -> None:
            events.append("grant_consumer_close")

    monkeypatch.setattr(app_module, "RvaSessionRegistry", HungRegistry)
    monkeypatch.setattr(app_module, "WorkerHeartbeatLoop", FastHeartbeat)
    app = create_app(
        settings(shutdown_drain_timeout_seconds=0.01),
        grant_consumer=FakeGrantConsumer(),  # type: ignore[arg-type]
    )

    with TestClient(app):
        pass

    assert set(events) == {
        "registry_close_started",
        "registry_child_reaped",
        "final_release_sent",
        "heartbeat_close",
        "grant_consumer_close",
    }
    assert events.index("registry_close_started") < events.index("final_release_sent")
    assert events.index("final_release_sent") < events.index("heartbeat_close")


def test_worker_shutdown_stops_when_release_acknowledgements_never_arrive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = LeaseRenewal(
        tenant_id="tenant-1",
        device_id="device-1",
        session_epoch="epoch-1",
        fencing_token=2,
    )
    heartbeat_calls = 0

    class PendingRegistry:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

        async def revoke_expired_leases(self, _now: float) -> None:
            return None

        async def revoke_session_epochs(self, _session_epochs: set[str]) -> None:
            return None

        def extend_lease_deadlines(self, _expires_at: float, _rejected_epochs: set[str]) -> None:
            return None

        def active_lease_renewals(self) -> tuple[LeaseRenewal, ...]:
            return ()

        def pending_lease_releases(self) -> tuple[LeaseRenewal, ...]:
            return (release,) if self.closed else ()

        def acknowledge_lease_releases(self, _acknowledged: tuple[LeaseRenewal, ...]) -> None:
            return None

    class NonAcknowledgingHeartbeat:
        last_success = True

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return None

        def start(self) -> None:
            return None

        async def send_once(self) -> None:
            nonlocal heartbeat_calls
            heartbeat_calls += 1

        async def close(self) -> None:
            return None

    class FakeGrantConsumer:
        async def close(self) -> None:
            return None

    monkeypatch.setattr(app_module, "RvaSessionRegistry", PendingRegistry)
    monkeypatch.setattr(app_module, "WorkerHeartbeatLoop", NonAcknowledgingHeartbeat)
    app = create_app(settings(), grant_consumer=FakeGrantConsumer())  # type: ignore[arg-type]

    with TestClient(app):
        pass

    assert heartbeat_calls == app_module.SHUTDOWN_RELEASE_MAX_ATTEMPTS + 1


def test_worker_snapshots_active_leases_before_blackhole_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    releases: list[LeaseRenewal] = []
    release_blackhole: asyncio.Event | None = None
    baseline = detached_shutdown_task_count()
    lease = LeaseRenewal(
        tenant_id="tenant-1",
        device_id="device-1",
        session_epoch="epoch-1",
        fencing_token=2,
    )

    class SnapshotRegistry:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return None

        def snapshot_active_lease_releases(self) -> None:
            events.append("snapshot")
            releases.append(lease)

        async def close(self) -> None:
            events.append("registry_close")

        async def revoke_expired_leases(self, _now: float) -> None:
            return None

        async def revoke_session_epochs(self, _session_epochs: set[str]) -> None:
            return None

        def extend_lease_deadlines(self, _expires_at: float, _rejected_epochs: set[str]) -> None:
            return None

        def active_lease_renewals(self) -> tuple[LeaseRenewal, ...]:
            return (lease,)

        def pending_lease_releases(self) -> tuple[LeaseRenewal, ...]:
            return tuple(releases)

        def acknowledge_lease_releases(self, _acknowledged: tuple[LeaseRenewal, ...]) -> None:
            releases.clear()

    class BlackholeHeartbeat:
        last_success = True

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            nonlocal release_blackhole
            release_blackhole = asyncio.Event()

        def start(self) -> None:
            return None

        async def send_once(self) -> None:
            events.append("heartbeat_started")
            assert release_blackhole is not None
            while not release_blackhole.is_set():
                try:
                    await release_blackhole.wait()
                except asyncio.CancelledError:
                    continue

        async def close(self) -> None:
            events.append("heartbeat_close")
            assert release_blackhole is not None
            release_blackhole.set()

    class FakeGrantConsumer:
        async def close(self) -> None:
            return None

    monkeypatch.setattr(app_module, "RvaSessionRegistry", SnapshotRegistry)
    monkeypatch.setattr(app_module, "WorkerHeartbeatLoop", BlackholeHeartbeat)
    app = create_app(
        settings(shutdown_drain_timeout_seconds=0.02),
        grant_consumer=FakeGrantConsumer(),  # type: ignore[arg-type]
    )

    with TestClient(app):
        pass

    assert events[0:2] == ["snapshot", "heartbeat_started"]
    assert "heartbeat_close" in events
    assert releases == [lease]
    assert detached_shutdown_task_count() == baseline


@pytest.mark.asyncio
async def test_registry_startup_abort_is_idempotent_for_admission_and_exact_release(
) -> None:
    admission = SharedSessionAdmission(1)
    registry = RvaSessionRegistry(settings(), admission)
    auth = AuthContext(
        tenant_id="tenant-1",
        device_id="device-1",
        allowed_profiles=("wss-opus-v3",),
        session_epoch="epoch-1",
        fencing_token=7,
        expires_at=100.0,
    )
    reservation = await registry.reserve(auth)
    assert reservation is not None

    await registry.abort_startup(auth, reservation)
    await registry.abort_startup(auth, reservation)

    assert admission.active_count == 0
    assert registry.pending_lease_releases() == (
        LeaseRenewal(
            tenant_id="tenant-1",
            device_id="device-1",
            session_epoch="epoch-1",
            fencing_token=7,
        ),
    )


async def test_worker_heartbeat_reports_capacity_and_applies_director_drain() -> None:
    requests: list[dict[str, object]] = []
    revoked: list[tuple[LeaseRenewal, ...]] = []

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

        async def lease_renewal_deadline(
            self,
            renewals: tuple[LeaseRenewal, ...],
        ) -> float | None:
            assert renewals == self.active_lease_renewals()
            return None

        async def revoke_lease_claims(self, claims: tuple[LeaseRenewal, ...]) -> None:
            revoked.append(claims)

        async def revoke_expired_leases(self, now: float) -> None:
            assert now > 0

        async def extend_lease_deadlines(
            self,
            expires_at: float,
            accepted: tuple[LeaseRenewal, ...],
        ) -> None:
            assert expires_at == 153.0
            assert accepted == ()

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
        rva_public_ws_url="ws://worker-a.test/v2/voice",
    )
    admission = SharedSessionAdmission(worker_settings.max_sessions)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        heartbeat = WorkerHeartbeatLoop(worker_settings, admission, FakeRegistry(), client=client)  # type: ignore[arg-type]
        await heartbeat.send_once()

    assert requests[0]["worker_id"] == "worker-a"
    assert requests[0]["max_sessions"] == 5
    assert requests[0]["active_sessions"] == 0
    assert requests[0]["healthy"] is True
    assert requests[0]["profiles"] == ["wss-opus-v3"]
    assert requests[0]["bindings"] == [
        {
            "control_protocol": "rva-control-v2",
            "public_wss_url": "ws://worker-a.test/v2/voice",
            "profiles": ["wss-opus-v3"],
        },
    ]
    assert requests[0]["active_leases"] == [
        {
            "tenant_id": "tenant-1",
            "device_id": "device-1",
            "session_epoch": "epoch-1",
            "fencing_token": 2,
        }
    ]
    assert admission.draining is True
    assert revoked == [
        (
            LeaseRenewal(
                tenant_id="tenant-1",
                device_id="device-1",
                session_epoch="epoch-1",
                fencing_token=2,
            ),
        )
    ]


@pytest.mark.asyncio
async def test_worker_heartbeat_does_not_clear_local_drain() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "accepted": True,
                "draining": False,
                "heartbeat_expires_at": 123.0,
                "lease_expires_at": 153.0,
                "rejected_session_epochs": [],
            },
        )

    worker_settings = settings(director_url="http://director.test", heartbeat_enabled=True)
    admission = SharedSessionAdmission(worker_settings.max_sessions)
    admission.set_draining(True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        heartbeat = WorkerHeartbeatLoop(worker_settings, admission, client=client)
        await heartbeat.send_once()

    assert admission.draining is True


@pytest.mark.asyncio
async def test_heartbeat_with_failed_udp_gateway_removes_udp_and_reports_unhealthy() -> None:
    requests: list[dict[str, object]] = []

    class FailedUdpGateway:
        is_ready = False

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.read()))
        return httpx.Response(
            200,
            json={
                "accepted": True,
                "draining": False,
                "heartbeat_expires_at": 123.0,
                "lease_expires_at": 153.0,
                "rejected_session_epochs": [],
            },
        )

    worker_settings = settings(
        director_url="http://director.test",
        heartbeat_enabled=True,
        rva_udp_enabled=True,
        udp_advertise_host="voice.example.test",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        heartbeat = WorkerHeartbeatLoop(
            worker_settings,
            SharedSessionAdmission(5),
            udp_gateway=FailedUdpGateway(),  # type: ignore[arg-type]
            client=client,
        )
        await heartbeat.send_once()

    assert requests[0]["healthy"] is False
    assert requests[0]["profiles"] == ["wss-opus-v3"]
    assert all(
        "udp-opus-gcm-v2" not in binding["profiles"]
        for binding in requests[0]["bindings"]  # type: ignore[union-attr]
    )


@pytest.mark.asyncio
async def test_rva_registry_holds_admission_until_connection_cleanup_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_release = asyncio.Event()

    class FakeConnection:
        async def run(self) -> None:
            return None

        async def wait_closed(self) -> None:
            await cleanup_release.wait()

    monkeypatch.setattr(app_module, "RvaWssConnection", lambda *args, **kwargs: FakeConnection())
    admission = SharedSessionAdmission(1)
    registry = RvaSessionRegistry(settings(), admission)
    auth = AuthContext(tenant_id="tenant-1", device_id="device-1")
    reservation = await registry.reserve(auth)
    assert reservation is not None

    task = asyncio.create_task(registry.run(object(), auth, reservation))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    assert admission.active_count == 1
    assert await registry.reserve(auth) is None

    cleanup_release.set()
    await task
    assert admission.active_count == 0


@pytest.mark.asyncio
async def test_registry_close_fences_concurrent_registration_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retirement_started = asyncio.Event()
    retirement_release = asyncio.Event()
    websocket_closes: list[tuple[int, str]] = []
    constructed = 0
    owned_close_calls = 0

    class OwnedConnection:
        async def close(self, *, code: int, reason: str) -> None:
            nonlocal owned_close_calls
            assert (code, reason) == (1001, "server_shutdown")
            owned_close_calls += 1
            retirement_started.set()

        async def wait_closed(self) -> None:
            await retirement_release.wait()

    class AcceptedWebSocket:
        async def close(self, *, code: int, reason: str) -> None:
            websocket_closes.append((code, reason))

    def build_connection(*_args: object, **_kwargs: object) -> object:
        nonlocal constructed
        constructed += 1
        raise AssertionError("late run must not construct a connection")

    admission = SharedSessionAdmission(2)
    registry = RvaSessionRegistry(settings(), admission)
    owned_auth = AuthContext(tenant_id="tenant-0", device_id="device-0")
    registry._connections[("tenant-0", "device-0")] = (  # type: ignore[assignment]  # noqa: SLF001
        OwnedConnection(),
        owned_auth,
    )
    late_auth = AuthContext(
        tenant_id="tenant-1",
        device_id="device-1",
        session_epoch="epoch-1",
        fencing_token=7,
        expires_at=200.0,
    )
    reservation = await registry.reserve(late_auth)
    assert reservation is not None
    monkeypatch.setattr(app_module, "RvaWssConnection", build_connection)

    first_close = asyncio.create_task(registry.close())
    await asyncio.wait_for(retirement_started.wait(), timeout=1.0)
    first_close.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_close
    assert registry._close_task is not None  # noqa: SLF001
    assert not registry._close_task.done()  # noqa: SLF001
    second_close = asyncio.create_task(registry.close())
    try:
        await registry.run(AcceptedWebSocket(), late_auth, reservation)  # type: ignore[arg-type]
        await asyncio.sleep(0)
        assert not second_close.done()
        assert constructed == 0
        assert websocket_closes == [(1001, "server_shutdown")]
        assert admission.active_count == 0
        assert registry.pending_lease_releases() == (
            LeaseRenewal(
                tenant_id="tenant-1",
                device_id="device-1",
                session_epoch="epoch-1",
                fencing_token=7,
            ),
        )
        assert registry._connections == {}  # noqa: SLF001
        assert registry._lease_deadlines == {}  # noqa: SLF001
        assert registry._expiry_task is None  # noqa: SLF001
    finally:
        retirement_release.set()
        await asyncio.gather(first_close, second_close, return_exceptions=True)

    assert owned_close_calls == 1
    assert registry._closed is True  # noqa: SLF001
    assert registry._retirement_tasks == {}  # noqa: SLF001


@pytest.mark.asyncio
async def test_registry_close_owner_exception_is_consumed_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    registry = RvaSessionRegistry(settings(), SharedSessionAdmission(1))

    async def fail() -> None:
        raise RuntimeError("close owner failed")

    with caplog.at_level(logging.ERROR):
        task = asyncio.create_task(fail())
        task.add_done_callback(registry._close_owner_done)  # noqa: SLF001
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

    assert any(
        record.message
        == "worker_registry_close_owner_failed action=fail_closed error_type=RuntimeError"
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_rva_registry_does_not_drop_release_claims_above_heartbeat_batch_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeConnection:
        async def run(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    monkeypatch.setattr(app_module, "RvaWssConnection", lambda *args, **kwargs: FakeConnection())
    admission = SharedSessionAdmission(80)
    registry = RvaSessionRegistry(settings(max_sessions=80), admission)

    for index in range(80):
        auth = AuthContext(
            tenant_id="tenant-1",
            device_id=f"device-{index}",
            session_epoch=f"epoch-{index}",
            fencing_token=1,
            expires_at=200.0,
        )
        reservation = await registry.reserve(auth)
        assert reservation is not None
        await registry.run(object(), auth, reservation)  # type: ignore[arg-type]

    assert len(registry.pending_lease_releases()) == 80
    assert registry._expiry_task is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_successful_heartbeat_renews_lease_before_local_deadline_without_closing() -> None:
    clock = MutableClock(99.0)
    registry = RvaSessionRegistry(settings(), SharedSessionAdmission(5), clock=clock)
    closed: list[tuple[int, str]] = []

    class FakeConnection:
        async def close(self, *, code: int, reason: str) -> None:
            closed.append((code, reason))

        async def wait_closed(self) -> None:
            return None

    auth = AuthContext(
        tenant_id="tenant-1",
        device_id="device-1",
        session_epoch="epoch-1",
        fencing_token=1,
        expires_at=100.0,
    )
    registry._connections[("tenant-1", "device-1")] = (FakeConnection(), auth)  # type: ignore[assignment]  # noqa: SLF001
    registry._lease_deadlines[("tenant-1", "device-1", "epoch-1", 1)] = 100.0  # noqa: SLF001

    def renewed(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read())
        assert payload["active_leases"] == [
            {
                "tenant_id": "tenant-1",
                "device_id": "device-1",
                "session_epoch": "epoch-1",
                "fencing_token": 1,
            }
        ]
        return httpx.Response(
            200,
            json={
                "accepted": True,
                "draining": False,
                "heartbeat_expires_at": 115.0,
                "lease_expires_at": 130.0,
                "rejected_session_epochs": [],
            },
        )

    worker_settings = settings(director_url="http://director.test", heartbeat_enabled=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(renewed)) as client:
        heartbeat = WorkerHeartbeatLoop(
            worker_settings,
            SharedSessionAdmission(5),
            registry,
            client=client,
            clock=clock,
        )
        await heartbeat.send_once()

    assert closed == []
    assert registry._lease_deadlines == {("tenant-1", "device-1", "epoch-1", 1): 130.0}  # noqa: SLF001


@pytest.mark.asyncio
async def test_failed_heartbeat_enforces_local_deadline_after_renewal_attempt() -> None:
    clock = MutableClock(99.0)
    registry = RvaSessionRegistry(settings(), SharedSessionAdmission(5), clock=clock)
    events: list[str] = []
    closed: list[tuple[int, str]] = []
    connection_closed = asyncio.Event()

    class FakeConnection:
        async def close(self, *, code: int, reason: str) -> None:
            events.append("close")
            closed.append((code, reason))
            connection_closed.set()

        async def wait_closed(self) -> None:
            return None

    auth = AuthContext(
        tenant_id="tenant-1",
        device_id="device-1",
        session_epoch="epoch-1",
        fencing_token=1,
        expires_at=100.0,
    )
    registry._connections[("tenant-1", "device-1")] = (FakeConnection(), auth)  # type: ignore[assignment]  # noqa: SLF001
    registry._lease_deadlines[("tenant-1", "device-1", "epoch-1", 1)] = 100.0  # noqa: SLF001

    def unavailable(_request: httpx.Request) -> httpx.Response:
        events.append("renew")
        clock.advance(1.0)
        raise httpx.ConnectError("director unavailable")

    worker_settings = settings(director_url="http://director.test", heartbeat_enabled=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
        heartbeat = WorkerHeartbeatLoop(
            worker_settings,
            SharedSessionAdmission(5),
            registry,
            client=client,
            clock=clock,
        )
        with pytest.raises(httpx.ConnectError):
            await heartbeat.send_once()
    await asyncio.wait_for(connection_closed.wait(), timeout=1.0)
    assert events == ["renew", "close"]
    assert closed == [(1008, "stale_route_lease")]


@pytest.mark.asyncio
async def test_blocked_heartbeat_cannot_outlive_submitted_lease_deadline() -> None:
    deadline = time.time() + 0.25
    registry = RvaSessionRegistry(settings(), SharedSessionAdmission(5))
    request_started = asyncio.Event()
    request_cancelled = asyncio.Event()
    connection_closed = asyncio.Event()
    close_calls = 0

    class FakeConnection:
        async def close(self, *, code: int, reason: str) -> None:
            nonlocal close_calls
            assert (code, reason) == (1008, "stale_route_lease")
            close_calls += 1
            connection_closed.set()

        async def wait_closed(self) -> None:
            return None

    auth = AuthContext(
        tenant_id="tenant-1",
        device_id="device-1",
        session_epoch="epoch-1",
        fencing_token=1,
        expires_at=deadline,
    )
    registry._connections[("tenant-1", "device-1")] = (FakeConnection(), auth)  # type: ignore[assignment]  # noqa: SLF001
    registry._lease_deadlines[("tenant-1", "device-1", "epoch-1", 1)] = deadline  # noqa: SLF001
    registry.start_expiry_enforcement()

    async def blocked(_request: httpx.Request) -> httpx.Response:
        request_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            request_cancelled.set()
        raise AssertionError("unreachable")

    worker_settings = settings(director_url="http://director.test", heartbeat_enabled=True)
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(blocked)) as client:
            heartbeat = WorkerHeartbeatLoop(
                worker_settings,
                SharedSessionAdmission(5),
                registry,
                client=client,
            )
            heartbeat_task = asyncio.create_task(heartbeat.send_once())
            await asyncio.wait_for(request_started.wait(), timeout=1.0)
            with pytest.raises(TimeoutError):
                await heartbeat_task

        await asyncio.wait_for(connection_closed.wait(), timeout=1.0)
        assert request_cancelled.is_set()
        assert close_calls == 1
    finally:
        await registry.close()

    assert registry._expiry_task is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_expiry_loop_does_not_wait_for_previous_connection_retirement() -> None:
    clock = MutableClock(100.0)
    registry = RvaSessionRegistry(settings(), SharedSessionAdmission(2), clock=clock)
    first_close_started = asyncio.Event()
    first_retirement_release = asyncio.Event()
    second_close_started = asyncio.Event()

    class FirstConnection:
        async def close(self, *, code: int, reason: str) -> None:
            assert (code, reason) == (1008, "stale_route_lease")
            first_close_started.set()

        async def wait_closed(self) -> None:
            await first_retirement_release.wait()

    class SecondConnection:
        async def close(self, *, code: int, reason: str) -> None:
            assert (code, reason) == (1008, "stale_route_lease")
            second_close_started.set()

        async def wait_closed(self) -> None:
            return None

    first_auth = AuthContext(
        tenant_id="tenant-1",
        device_id="device-1",
        session_epoch="epoch-1",
        fencing_token=1,
        expires_at=100.0,
    )
    second_auth = AuthContext(
        tenant_id="tenant-2",
        device_id="device-2",
        session_epoch="epoch-2",
        fencing_token=2,
        expires_at=200.0,
    )
    registry._connections[("tenant-1", "device-1")] = (  # type: ignore[assignment]  # noqa: SLF001
        FirstConnection(),
        first_auth,
    )
    registry._connections[("tenant-2", "device-2")] = (  # type: ignore[assignment]  # noqa: SLF001
        SecondConnection(),
        second_auth,
    )
    registry._lease_deadlines[("tenant-1", "device-1", "epoch-1", 1)] = 100.0  # noqa: SLF001
    registry._lease_deadlines[("tenant-2", "device-2", "epoch-2", 2)] = 200.0  # noqa: SLF001
    registry.start_expiry_enforcement()

    try:
        await asyncio.wait_for(first_close_started.wait(), timeout=1.0)
        assert not second_close_started.is_set()
        clock.advance(100.0)
        registry._expiry_changed.set()  # noqa: SLF001
        await asyncio.wait_for(second_close_started.wait(), timeout=1.0)
        assert registry._connections == {}  # noqa: SLF001
        assert registry._lease_deadlines == {}  # noqa: SLF001
    finally:
        first_retirement_release.set()
        await registry.close()


@pytest.mark.asyncio
async def test_expiry_enforcement_failure_is_critical_and_fails_closed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    closed = asyncio.Event()

    class BrokenClock:
        def __call__(self) -> float:
            raise RuntimeError("clock failed")

    class FakeConnection:
        async def close(self, *, code: int, reason: str) -> None:
            assert (code, reason) == (1008, "stale_route_lease")
            closed.set()

        async def wait_closed(self) -> None:
            return None

    registry = RvaSessionRegistry(
        settings(),
        SharedSessionAdmission(5),
        clock=BrokenClock(),
    )
    auth = AuthContext(
        tenant_id="tenant-1",
        device_id="device-1",
        session_epoch="epoch-1",
        fencing_token=1,
        expires_at=100.0,
    )
    registry._connections[("tenant-1", "device-1")] = (FakeConnection(), auth)  # type: ignore[assignment]  # noqa: SLF001
    registry._lease_deadlines[("tenant-1", "device-1", "epoch-1", 1)] = 100.0  # noqa: SLF001

    try:
        with caplog.at_level(logging.CRITICAL):
            registry.start_expiry_enforcement()
            await asyncio.wait_for(closed.wait(), timeout=1.0)
    finally:
        await registry.close()

    assert any(
        record.message
        == "worker_lease_expiry_enforcement_failed action=fail_closed error_type=RuntimeError"
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_heartbeat_does_not_extend_session_added_while_request_is_awaiting() -> None:
    clock = MutableClock(100.0)
    registry = RvaSessionRegistry(settings(), SharedSessionAdmission(5), clock=clock)
    request_started = asyncio.Event()
    release_response = asyncio.Event()

    class FakeConnection:
        async def close(self, *, code: int, reason: str) -> None:
            raise AssertionError(f"unexpected close: {code} {reason}")

        async def wait_closed(self) -> None:
            return None

    submitted_auth = AuthContext(
        tenant_id="tenant-1",
        device_id="device-1",
        session_epoch="epoch-1",
        fencing_token=1,
        expires_at=150.0,
    )
    registry._connections[("tenant-1", "device-1")] = (FakeConnection(), submitted_auth)  # type: ignore[assignment]  # noqa: SLF001
    registry._lease_deadlines[("tenant-1", "device-1", "epoch-1", 1)] = 150.0  # noqa: SLF001

    async def renewed(_request: httpx.Request) -> httpx.Response:
        request_started.set()
        await release_response.wait()
        return httpx.Response(
            200,
            json={
                "accepted": True,
                "draining": False,
                "heartbeat_expires_at": 200.0,
                "lease_expires_at": 180.0,
                "rejected_session_epochs": [],
            },
        )

    worker_settings = settings(director_url="http://director.test", heartbeat_enabled=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(renewed)) as client:
        heartbeat = WorkerHeartbeatLoop(
            worker_settings,
            SharedSessionAdmission(5),
            registry,
            client=client,
            clock=clock,
        )
        heartbeat_task = asyncio.create_task(heartbeat.send_once())
        await asyncio.wait_for(request_started.wait(), timeout=1.0)

        new_auth = AuthContext(
            tenant_id="tenant-2",
            device_id="device-2",
            session_epoch="epoch-2",
            fencing_token=2,
            expires_at=160.0,
        )
        async with registry._lock:  # noqa: SLF001
            registry._connections[("tenant-2", "device-2")] = (FakeConnection(), new_auth)  # type: ignore[assignment]  # noqa: SLF001
            registry._lease_deadlines[("tenant-2", "device-2", "epoch-2", 2)] = 160.0  # noqa: SLF001
        release_response.set()
        await heartbeat_task

    assert registry._lease_deadlines == {  # noqa: SLF001
        ("tenant-1", "device-1", "epoch-1", 1): 180.0,
        ("tenant-2", "device-2", "epoch-2", 2): 160.0,
    }


@pytest.mark.asyncio
async def test_heartbeat_response_at_deadline_cannot_resurrect_expired_claim() -> None:
    clock = MutableClock(99.0)
    registry = RvaSessionRegistry(settings(), SharedSessionAdmission(5), clock=clock)
    closed: list[tuple[int, str]] = []
    connection_closed = asyncio.Event()

    class FakeConnection:
        async def close(self, *, code: int, reason: str) -> None:
            closed.append((code, reason))
            connection_closed.set()

        async def wait_closed(self) -> None:
            return None

    auth = AuthContext(
        tenant_id="tenant-1",
        device_id="device-1",
        session_epoch="epoch-1",
        fencing_token=1,
        expires_at=100.0,
    )
    registry._connections[("tenant-1", "device-1")] = (FakeConnection(), auth)  # type: ignore[assignment]  # noqa: SLF001
    registry._lease_deadlines[("tenant-1", "device-1", "epoch-1", 1)] = 100.0  # noqa: SLF001

    def at_deadline(_request: httpx.Request) -> httpx.Response:
        clock.advance(1.0)
        return httpx.Response(
            200,
            json={
                "accepted": True,
                "draining": False,
                "heartbeat_expires_at": 120.0,
                "lease_expires_at": 130.0,
                "rejected_session_epochs": [],
            },
        )

    worker_settings = settings(director_url="http://director.test", heartbeat_enabled=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(at_deadline)) as client:
        heartbeat = WorkerHeartbeatLoop(
            worker_settings,
            SharedSessionAdmission(5),
            registry,
            client=client,
            clock=clock,
        )
        await heartbeat.send_once()

    await asyncio.wait_for(connection_closed.wait(), timeout=1.0)
    assert closed == [(1008, "stale_route_lease")]
    assert registry._lease_deadlines == {}  # noqa: SLF001


@pytest.mark.asyncio
async def test_rejected_submitted_claim_does_not_close_new_epoch_for_same_principal() -> None:
    clock = MutableClock(100.0)
    registry = RvaSessionRegistry(settings(), SharedSessionAdmission(5), clock=clock)
    request_started = asyncio.Event()
    release_response = asyncio.Event()
    new_closed: list[tuple[int, str]] = []

    class OldConnection:
        async def close(self, *, code: int, reason: str) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    class NewConnection:
        async def close(self, *, code: int, reason: str) -> None:
            new_closed.append((code, reason))

        async def wait_closed(self) -> None:
            return None

    old_auth = AuthContext(
        tenant_id="tenant-1",
        device_id="device-1",
        session_epoch="epoch-1",
        fencing_token=1,
        expires_at=150.0,
    )
    registry._connections[("tenant-1", "device-1")] = (OldConnection(), old_auth)  # type: ignore[assignment]  # noqa: SLF001
    registry._lease_deadlines[("tenant-1", "device-1", "epoch-1", 1)] = 150.0  # noqa: SLF001

    async def rejected(_request: httpx.Request) -> httpx.Response:
        request_started.set()
        await release_response.wait()
        return httpx.Response(
            200,
            json={
                "accepted": True,
                "draining": False,
                "heartbeat_expires_at": 160.0,
                "lease_expires_at": 170.0,
                "rejected_session_epochs": ["epoch-1"],
            },
        )

    worker_settings = settings(director_url="http://director.test", heartbeat_enabled=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(rejected)) as client:
        heartbeat = WorkerHeartbeatLoop(
            worker_settings,
            SharedSessionAdmission(5),
            registry,
            client=client,
            clock=clock,
        )
        heartbeat_task = asyncio.create_task(heartbeat.send_once())
        await asyncio.wait_for(request_started.wait(), timeout=1.0)

        new_auth = AuthContext(
            tenant_id="tenant-1",
            device_id="device-1",
            session_epoch="epoch-2",
            fencing_token=2,
            expires_at=160.0,
        )
        async with registry._lock:  # noqa: SLF001
            registry._connections[("tenant-1", "device-1")] = (NewConnection(), new_auth)  # type: ignore[assignment]  # noqa: SLF001
            registry._lease_deadlines.pop(("tenant-1", "device-1", "epoch-1", 1))
            registry._lease_deadlines[("tenant-1", "device-1", "epoch-2", 2)] = 160.0  # noqa: SLF001
        release_response.set()
        await heartbeat_task

    assert new_closed == []
    assert registry._lease_deadlines == {  # noqa: SLF001
        ("tenant-1", "device-1", "epoch-2", 2): 160.0,
    }


@pytest.mark.asyncio
async def test_malformed_success_heartbeat_expires_locally_without_acknowledging_releases() -> None:
    release = LeaseRenewal(
        tenant_id="tenant-1",
        device_id="device-1",
        session_epoch="epoch-1",
        fencing_token=1,
    )
    events: list[str] = []

    class FakeRegistry:
        def pending_lease_releases(self) -> tuple[LeaseRenewal, ...]:
            return (release,)

        def active_lease_renewals(self) -> tuple[LeaseRenewal, ...]:
            return ()

        async def lease_renewal_deadline(
            self,
            renewals: tuple[LeaseRenewal, ...],
        ) -> float | None:
            return None

        def acknowledge_lease_releases(self, _releases: tuple[LeaseRenewal, ...]) -> None:
            events.append("acknowledge")

        async def revoke_expired_leases(self, now: float) -> None:
            assert now == 200.0
            events.append("expire")

    def malformed(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"accepted": True, "draining": False})

    clock = MutableClock(200.0)
    worker_settings = settings(director_url="http://director.test", heartbeat_enabled=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(malformed)) as client:
        heartbeat = WorkerHeartbeatLoop(
            worker_settings,
            SharedSessionAdmission(5),
            FakeRegistry(),  # type: ignore[arg-type]
            client=client,
            clock=clock,
        )
        with pytest.raises(ValueError):
            await heartbeat.send_once()

    assert events == ["expire"]


@pytest.mark.asyncio
async def test_registry_closes_connection_after_local_lease_deadline() -> None:
    clock = MutableClock(100.0)
    registry = RvaSessionRegistry(settings(), SharedSessionAdmission(5), clock=clock)
    closed: list[tuple[int, str]] = []
    connection_closed = asyncio.Event()

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
            connection_closed.set()

        async def wait_closed(self) -> None:
            return None

    auth = FakeConnection.auth_context
    registry._connections[("tenant-1", "device-1")] = (FakeConnection(), auth)  # type: ignore[assignment]  # noqa: SLF001
    registry._lease_deadlines[("tenant-1", "device-1", "epoch-1", 1)] = 100.0  # noqa: SLF001
    await registry.revoke_expired_leases(100.0)
    await asyncio.wait_for(connection_closed.wait(), timeout=1.0)
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


@pytest.mark.asyncio
async def test_grant_consumer_logs_director_reject_reason_without_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def rejected(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "grant_rejected", "reason": "grant_replay"})

    token = "signed-" + "token-with-sensitive-payload"
    worker_settings = settings(director_url="http://director.test")
    caplog.set_level(logging.WARNING, logger="realtime_worker.app")
    async with httpx.AsyncClient(transport=httpx.MockTransport(rejected)) as client:
        consumer = DirectorGrantConsumer(worker_settings, client=client)
        accepted = await consumer.consume(token, device_id="device-1")

    assert accepted is False
    assert "director_grant_consume_rejected reason=grant_replay status_code=409" in caplog.text
    assert "device_ref=" in caplog.text
    assert token not in caplog.text
    assert "device-1" not in caplog.text
    assert "validator-internal-token" not in caplog.text


def test_live_provider_worker_is_not_ready_when_network_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unavailable(_url: str, *, timeout: float) -> bool:
        assert timeout > 0
        return False

    monkeypatch.setattr(app_module, "_probe_endpoint", unavailable)
    live_settings = settings(
        runner="livekit",
        deepseek_api_key="validator-provider-key",
        provider_readiness_required=True,
    )
    app = create_app(live_settings)
    with TestClient(app) as client:
        ready = client.get("/health/ready")
        assert ready.status_code == 503
        assert ready.json()["provider_network_checked"] is True
        assert ready.json()["provider_network_ready"] is False


def test_worker_is_not_ready_until_director_heartbeat_succeeds() -> None:
    worker_settings = settings(director_url="http://director.test", heartbeat_enabled=True)
    app = create_app(worker_settings)
    with TestClient(app) as client:
        heartbeat = app.state.worker_heartbeat
        heartbeat.last_success = False
        assert client.get("/health/ready").status_code == 503
        heartbeat.last_success = True
        assert client.get("/health/ready").status_code == 200
