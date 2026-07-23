from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient
from realtime_worker import app as app_module
from realtime_worker.admission import SharedSessionAdmission
from realtime_worker.app import DirectorGrantConsumer, RvaSessionRegistry, WorkerHeartbeatLoop, create_app
from realtime_worker.auth import AuthContext
from realtime_worker.bindings.xiaozhi import XiaozhiSessionRegistry
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
        "xiaozhi_udp_enabled": False,
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


def test_shared_udp_gateway_accepts_deprecated_xiaozhi_environment_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "VOICE_UDP_BIND_HOST",
        "VOICE_UDP_BIND_PORT",
        "VOICE_UDP_ADVERTISE_HOST",
        "VOICE_UDP_ADVERTISE_PORT",
        "VOICE_UDP_PROBE_TIMEOUT_SECONDS",
        "VOICE_UDP_SESSION_LIFETIME_SECONDS",
        "VOICE_UDP_QUEUE_DATAGRAMS",
        "VOICE_UDP_REORDER_WAIT_MS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("VOICE_XIAOZHI_UDP_BIND_HOST", "127.0.0.3")
    monkeypatch.setenv("VOICE_XIAOZHI_UDP_BIND_PORT", "18093")
    monkeypatch.setenv("VOICE_XIAOZHI_UDP_ADVERTISE_HOST", "legacy.example.test")
    monkeypatch.setenv("VOICE_XIAOZHI_UDP_ADVERTISE_PORT", "28093")
    monkeypatch.setenv("VOICE_XIAOZHI_UDP_PROBE_TIMEOUT_SECONDS", "4")
    monkeypatch.setenv("VOICE_XIAOZHI_UDP_SESSION_LIFETIME_SECONDS", "700")
    monkeypatch.setenv("VOICE_XIAOZHI_UDP_QUEUE_DATAGRAMS", "40")
    monkeypatch.setenv("VOICE_XIAOZHI_UDP_REORDER_WAIT_MS", "35")

    value = Settings(_env_file=None)

    assert value.udp_bind_host == "127.0.0.3"
    assert value.udp_bind_port == 18093
    assert value.udp_advertise_host == "legacy.example.test"
    assert value.udp_advertise_port == 28093
    assert value.udp_probe_timeout_seconds == 4
    assert value.udp_session_lifetime_seconds == 700
    assert value.udp_queue_datagrams == 40
    assert value.udp_reorder_wait_ms == 35


def test_shared_udp_gateway_prefers_neutral_environment_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOICE_UDP_BIND_PORT", "18092")
    monkeypatch.setenv("VOICE_XIAOZHI_UDP_BIND_PORT", "18093")

    assert Settings(_env_file=None).udp_bind_port == 18092


def test_worker_exposes_only_rva_and_health_by_default() -> None:
    app = create_app(settings())
    paths = {route.path for route in app.routes}
    assert "/v1/xiaozhi" not in paths
    assert "/v1/voice" in paths
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
        assert ready.json()["legacy_xiaozhi_enabled"] is False


def test_legacy_xiaozhi_route_requires_explicit_enablement() -> None:
    app = create_app(settings(legacy_xiaozhi_enabled=True))

    assert "/v1/xiaozhi" in {route.path for route in app.routes}


def test_legacy_xiaozhi_udp_cannot_be_enabled_without_compatibility_binding() -> None:
    with pytest.raises(ValueError, match="VOICE_LEGACY_XIAOZHI_ENABLED"):
        create_app(settings(xiaozhi_udp_enabled=True, udp_advertise_host="voice.example.test"))


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
@pytest.mark.parametrize("registry_type", [RvaSessionRegistry, XiaozhiSessionRegistry])
async def test_registry_startup_abort_is_idempotent_for_admission_and_exact_release(
    registry_type: type[RvaSessionRegistry] | type[XiaozhiSessionRegistry],
) -> None:
    admission = SharedSessionAdmission(1)
    registry = registry_type(settings(), admission)
    auth = AuthContext(
        tenant_id="tenant-1",
        device_id="device-1",
        allowed_profiles=frozenset({"wss-opus-v2"}),
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
    assert requests[0]["profiles"] == ["wss-opus-v2"]
    assert requests[0]["bindings"] == [
        {
            "control_protocol": "rva-control-v1",
            "public_wss_url": "ws://127.0.0.1:8081/v1/voice",
            "profiles": ["wss-opus-v2"],
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
    assert revoked == [{"epoch-1"}]


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
async def test_heartbeat_advertises_legacy_binding_only_when_explicitly_enabled() -> None:
    requests: list[dict[str, object]] = []

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
        legacy_xiaozhi_enabled=True,
        worker_public_ws_url="ws://worker-a.test/v1/xiaozhi",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        heartbeat = WorkerHeartbeatLoop(worker_settings, SharedSessionAdmission(5), client=client)
        await heartbeat.send_once()

    assert requests[0]["profiles"] == ["wss-opus-v1", "wss-opus-v2"]
    assert [binding["control_protocol"] for binding in requests[0]["bindings"]] == [
        "xiaozhi-control-v1",
        "rva-control-v1",
    ]


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
    assert requests[0]["profiles"] == ["wss-opus-v2"]
    assert all(
        "udp-opus-gcm-v1" not in binding["profiles"]
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


@pytest.mark.asyncio
async def test_xiaozhi_registry_does_not_drop_release_claims_above_heartbeat_batch_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeConnection:
        def __init__(self, _websocket: object, auth: AuthContext, *_args: object, **_kwargs: object) -> None:
            self.auth_context = auth

        async def run(self) -> None:
            return None

    monkeypatch.setattr("realtime_worker.bindings.xiaozhi_runtime.XiaozhiConnection", FakeConnection)
    admission = SharedSessionAdmission(80)
    registry = XiaozhiSessionRegistry(settings(max_sessions=80), admission)

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
    live_settings = settings(runner="livekit", deepseek_api_key="validator-provider-key")
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
