from __future__ import annotations

import pytest
from session_director.service import DirectorService, GrantConsumeError, NoCapacityError
from session_director.store import InMemoryCoordinationStore, LeaseConflictError
from voice_contracts import BootstrapRequest, GrantCodec, LeaseRenewal, WorkerHeartbeat
from voice_testkit import MutableClock


def heartbeat(worker_id: str, active: int, *, maximum: int = 5, draining: bool = False) -> WorkerHeartbeat:
    return WorkerHeartbeat(
        worker_id=worker_id,
        public_wss_url=f"ws://{worker_id}.test/v1/xiaozhi",
        active_sessions=active,
        max_sessions=maximum,
        draining=draining,
        profiles=("wss-opus-v1", "udp-opus-gcm-v1"),
    )


@pytest.mark.asyncio
async def test_two_workers_select_capacity_and_respect_drain() -> None:
    clock = MutableClock()
    store = InMemoryCoordinationStore()
    service = DirectorService(
        store,
        GrantCodec("test-signing-key-with-32-bytes", clock=clock),
        heartbeat_ttl_seconds=30,
        lease_ttl_seconds=20,
        clock=clock,
    )
    await service.heartbeat(heartbeat("worker-a", 5))
    await service.heartbeat(heartbeat("worker-b", 2))

    selected = await service.bootstrap(BootstrapRequest(device_id="device-1"))
    assert selected.worker_id == "worker-b"

    await service.set_draining("worker-b", True)
    with pytest.raises(NoCapacityError):
        await service.bootstrap(BootstrapRequest(device_id="device-2"))


@pytest.mark.asyncio
async def test_route_lease_is_single_owner_and_fencing_increases_after_release() -> None:
    clock = MutableClock()
    store = InMemoryCoordinationStore()
    first = await store.acquire_route(
        tenant_id="tenant-1",
        device_id="device-1",
        worker_id="worker-a",
        worker_active_sessions=0,
        worker_max_sessions=5,
        session_epoch="epoch-1",
        now=clock(),
        expires_at=clock() + 10,
    )
    with pytest.raises(LeaseConflictError):
        await store.acquire_route(
            tenant_id="tenant-1",
            device_id="device-1",
            worker_id="worker-b",
            worker_active_sessions=0,
            worker_max_sessions=5,
            session_epoch="epoch-2",
            now=clock(),
            expires_at=clock() + 10,
        )

    assert await store.release_route(first) is True
    second = await store.acquire_route(
        tenant_id="tenant-1",
        device_id="device-1",
        worker_id="worker-b",
        worker_active_sessions=0,
        worker_max_sessions=5,
        session_epoch="epoch-2",
        now=clock(),
        expires_at=clock() + 10,
    )
    assert second.fencing_token == first.fencing_token + 1
    assert await store.release_route(first) is False


@pytest.mark.asyncio
async def test_colon_identifiers_do_not_collide_in_route_store() -> None:
    store = InMemoryCoordinationStore()
    first = await store.acquire_route(
        tenant_id="a:b",
        device_id="c",
        worker_id="worker-a",
        worker_active_sessions=0,
        worker_max_sessions=5,
        session_epoch="epoch-1",
        now=0,
        expires_at=10,
    )
    second = await store.acquire_route(
        tenant_id="a",
        device_id="b:c",
        worker_id="worker-a",
        worker_active_sessions=0,
        worker_max_sessions=5,
        session_epoch="epoch-2",
        now=0,
        expires_at=10,
    )
    assert first.route_key != second.route_key


@pytest.mark.asyncio
async def test_expired_heartbeat_and_full_worker_are_not_selected() -> None:
    clock = MutableClock()
    store = InMemoryCoordinationStore()
    service = DirectorService(
        store,
        GrantCodec("test-signing-key-with-32-bytes", clock=clock),
        heartbeat_ttl_seconds=5,
        lease_ttl_seconds=20,
        clock=clock,
    )
    await service.heartbeat(heartbeat("full-worker", 5))
    await service.heartbeat(heartbeat("expired-worker", 0))
    clock.advance(6)

    with pytest.raises(NoCapacityError):
        await service.bootstrap(BootstrapRequest(device_id="device-1"))


@pytest.mark.asyncio
async def test_pending_grants_fill_first_worker_then_spill_to_second() -> None:
    clock = MutableClock()
    store = InMemoryCoordinationStore()
    service = DirectorService(
        store,
        GrantCodec("test-signing-key-with-32-bytes", clock=clock),
        heartbeat_ttl_seconds=10,
        lease_ttl_seconds=20,
        clock=clock,
    )
    await service.heartbeat(heartbeat("worker-a", 0))
    await service.heartbeat(heartbeat("worker-b", 0))

    selected = [
        (await service.bootstrap(BootstrapRequest(device_id=f"device-{index}"))).worker_id for index in range(6)
    ]
    assert selected == ["worker-a"] * 5 + ["worker-b"]


@pytest.mark.asyncio
async def test_active_session_heartbeat_renews_lease_and_rejects_stale_fence() -> None:
    clock = MutableClock()
    store = InMemoryCoordinationStore()
    service = DirectorService(
        store,
        GrantCodec("test-signing-key-with-32-bytes", clock=clock),
        heartbeat_ttl_seconds=30,
        lease_ttl_seconds=20,
        clock=clock,
    )
    await service.heartbeat(heartbeat("worker-a", 0))
    opened = await service.bootstrap(BootstrapRequest(tenant_id="tenant-1", device_id="device-1"))

    clock.advance(10)
    renewed = await service.heartbeat(
        heartbeat("worker-a", 1).model_copy(
            update={
                "active_leases": (
                    LeaseRenewal(
                        tenant_id="tenant-1",
                        device_id="device-1",
                        session_epoch=opened.session_epoch,
                        fencing_token=opened.fencing_token,
                    ),
                )
            }
        )
    )
    assert renewed.rejected_session_epochs == ()

    clock.advance(11)
    with pytest.raises(LeaseConflictError):
        await service.bootstrap(BootstrapRequest(tenant_id="tenant-1", device_id="device-1"))

    stale = await service.heartbeat(
        heartbeat("worker-a", 1).model_copy(
            update={
                "active_leases": (
                    LeaseRenewal(
                        tenant_id="tenant-1",
                        device_id="device-1",
                        session_epoch="stale-epoch",
                        fencing_token=opened.fencing_token,
                    ),
                )
            }
        )
    )
    assert stale.rejected_session_epochs == ("stale-epoch",)

    await service.heartbeat(
        heartbeat("worker-a", 0).model_copy(
            update={
                "released_leases": (
                    LeaseRenewal(
                        tenant_id="tenant-1",
                        device_id="device-1",
                        session_epoch=opened.session_epoch,
                        fencing_token=opened.fencing_token,
                    ),
                )
            }
        )
    )
    reopened = await service.bootstrap(BootstrapRequest(tenant_id="tenant-1", device_id="device-1"))
    assert reopened.fencing_token == opened.fencing_token + 1


@pytest.mark.asyncio
async def test_grant_consumption_is_shared_single_use_and_route_bound() -> None:
    clock = MutableClock()
    store = InMemoryCoordinationStore()
    codec = GrantCodec("test-signing-key-with-32-bytes", clock=clock)
    service = DirectorService(
        store,
        codec,
        heartbeat_ttl_seconds=30,
        lease_ttl_seconds=20,
        clock=clock,
    )
    await service.heartbeat(heartbeat("worker-a", 0))
    opened = await service.bootstrap(BootstrapRequest(tenant_id="tenant-1", device_id="device-1"))

    consumed = await service.consume_grant(opened.connect_grant, worker_id="worker-a", device_id="device-1")
    assert consumed.session_epoch == opened.session_epoch
    restarted_service = DirectorService(
        store,
        codec,
        heartbeat_ttl_seconds=30,
        lease_ttl_seconds=20,
        clock=clock,
    )
    with pytest.raises(GrantConsumeError):
        await restarted_service.consume_grant(opened.connect_grant, worker_id="worker-a", device_id="device-1")

    second = await service.bootstrap(BootstrapRequest(tenant_id="tenant-1", device_id="device-2"))
    await store.release_route_claim(
        "worker-a",
        LeaseRenewal(
            tenant_id="tenant-1",
            device_id="device-2",
            session_epoch=second.session_epoch,
            fencing_token=second.fencing_token,
        ),
    )
    with pytest.raises(GrantConsumeError):
        await service.consume_grant(second.connect_grant, worker_id="worker-a", device_id="device-2")
