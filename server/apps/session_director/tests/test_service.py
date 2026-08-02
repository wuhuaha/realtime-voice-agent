from __future__ import annotations

import pytest
from session_director.service import DirectorService, GrantConsumeError, NoCapacityError
from session_director.store import InMemoryCoordinationStore, LeaseConflictError
from voice_contracts import (
    BindingAdvertisement,
    BootstrapRequest,
    GrantCodec,
    LeaseRenewal,
    RouteReleaseRequest,
    WorkerHeartbeat,
)
from voice_testkit import MutableClock


def heartbeat(worker_id: str, active: int, *, maximum: int = 5, draining: bool = False) -> WorkerHeartbeat:
    return WorkerHeartbeat(
        worker_id=worker_id,
        public_wss_url=f"ws://{worker_id}.test/rva/v1/voice",
        active_sessions=active,
        max_sessions=maximum,
        draining=draining,
        profiles=("wss-opus/1", "udp-opus-gcm/1"),
    )


@pytest.mark.asyncio
async def test_two_workers_select_capacity_and_respect_drain() -> None:
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
    await service.heartbeat(heartbeat("worker-a", 5))
    await service.heartbeat(heartbeat("worker-b", 2))

    selected = await service.bootstrap(BootstrapRequest(device_id="device-1"))
    assert selected.worker_id == "worker-b"

    await service.set_draining("worker-b", True)
    with pytest.raises(NoCapacityError):
        await service.bootstrap(BootstrapRequest(device_id="device-2"))


@pytest.mark.asyncio
async def test_bootstrap_selects_endpoint_for_requested_control_binding() -> None:
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
    await service.heartbeat(
        WorkerHeartbeat(
            worker_id="worker-a",
            public_wss_url="ws://worker-a.test/rva/v1/voice",
            active_sessions=0,
            profiles=("wss-opus/1", "udp-opus-gcm/1"),
            bindings=(
                BindingAdvertisement(
                    control_protocol="rva/1",
                    public_wss_url="ws://worker-a.test/rva/v1/voice",
                    profiles=("wss-opus/1", "udp-opus-gcm/1"),
                ),
            ),
        )
    )

    opened = await service.bootstrap(
        BootstrapRequest(
            device_id="device-rva",
            control_protocol="rva/1",
            supported_profiles=("wss-opus/1", "udp-opus-gcm/1"),
        )
    )
    claims = codec.verify(
        opened.connect_grant,
        worker_id="worker-a",
        device_id="device-rva",
    )

    assert opened.worker_wss_url == "ws://worker-a.test/rva/v1/voice"
    assert opened.control_protocol == "rva/1"
    assert opened.allowed_profiles == ("wss-opus/1", "udp-opus-gcm/1")
    assert claims.control_protocol == "rva/1"


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
async def test_device_release_allows_immediate_rebootstrap_and_stale_release_is_fenced() -> None:
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
    first = await service.bootstrap(BootstrapRequest(tenant_id="tenant-1", device_id="device-1"))

    assert await service.release(
        RouteReleaseRequest(
            tenant_id="tenant-1",
            device_id="device-1",
            worker_id=first.worker_id,
            session_epoch=first.session_epoch,
            fencing_token=first.fencing_token,
        )
    ) is True
    second = await service.bootstrap(BootstrapRequest(tenant_id="tenant-1", device_id="device-1"))
    assert second.fencing_token == first.fencing_token + 1

    assert await service.release(
        RouteReleaseRequest(
            tenant_id="tenant-1",
            device_id="device-1",
            worker_id=first.worker_id,
            session_epoch=first.session_epoch,
            fencing_token=first.fencing_token,
        )
    ) is False
    with pytest.raises(LeaseConflictError):
        await service.bootstrap(BootstrapRequest(tenant_id="tenant-1", device_id="device-1"))


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
async def test_heartbeat_stale_release_cannot_remove_new_epoch_on_same_route() -> None:
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
    first = await service.bootstrap(BootstrapRequest(tenant_id="tenant-1", device_id="device-1"))
    first_claim = LeaseRenewal(
        tenant_id="tenant-1",
        device_id="device-1",
        session_epoch=first.session_epoch,
        fencing_token=first.fencing_token,
    )
    assert await store.release_route_claim(first.worker_id, first_claim)
    second = await service.bootstrap(BootstrapRequest(tenant_id="tenant-1", device_id="device-1"))
    second_claim = LeaseRenewal(
        tenant_id="tenant-1",
        device_id="device-1",
        session_epoch=second.session_epoch,
        fencing_token=second.fencing_token,
    )

    result = await service.heartbeat(
        heartbeat("worker-a", 1).model_copy(
            update={
                "active_leases": (second_claim,),
                "released_leases": (first_claim,),
            }
        )
    )

    assert result.rejected_session_epochs == ()
    with pytest.raises(LeaseConflictError):
        await service.bootstrap(BootstrapRequest(tenant_id="tenant-1", device_id="device-1"))
    assert await store.release_route_claim(second.worker_id, second_claim)
    assert not await store.release_route_claim(second.worker_id, second_claim)
    third = await service.bootstrap(BootstrapRequest(tenant_id="tenant-1", device_id="device-1"))
    assert third.fencing_token == second.fencing_token + 1


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
    with pytest.raises(GrantConsumeError) as replayed:
        await restarted_service.consume_grant(opened.connect_grant, worker_id="worker-a", device_id="device-1")
    assert replayed.value.reason == "grant_replay"

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
    with pytest.raises(GrantConsumeError) as released_route:
        await service.consume_grant(second.connect_grant, worker_id="worker-a", device_id="device-2")
    assert released_route.value.reason == "no_route"
