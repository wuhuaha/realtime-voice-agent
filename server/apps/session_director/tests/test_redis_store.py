from __future__ import annotations

import os
import time
import uuid

import pytest
from redis.asyncio import Redis
from session_director.store import (
    LeaseConflictError,
    RedisCoordinationStore,
    WorkerCapacityError,
)
from voice_contracts import ConnectGrantClaims, LeaseRenewal, WorkerHeartbeat


@pytest.mark.integration
@pytest.mark.asyncio
async def test_redis_store_enforces_route_owner_capacity_and_fencing() -> None:
    url = os.getenv("VOICE_TEST_REDIS_URL")
    if not url:
        pytest.skip("VOICE_TEST_REDIS_URL is not configured")
    client = Redis.from_url(url, decode_responses=False)
    prefix = f"voice-test-{uuid.uuid4().hex}"
    store = RedisCoordinationStore(client, prefix=prefix)
    try:
        await store.heartbeat(
            WorkerHeartbeat(
                worker_id="worker-a",
                public_wss_url="ws://worker-a.test/rva/v1/voice",
                active_sessions=0,
                max_sessions=5,
            ),
            expires_at=1_700_000_030,
        )
        leases = []
        for index in range(5):
            leases.append(
                await store.acquire_route(
                    tenant_id="tenant-1",
                    device_id=f"device-{index}",
                    worker_id="worker-a",
                    worker_active_sessions=0,
                    worker_max_sessions=5,
                    session_epoch=f"epoch-{index}",
                    now=1_700_000_000,
                    expires_at=1_700_000_020,
                )
            )
        with pytest.raises(WorkerCapacityError):
            await store.acquire_route(
                tenant_id="tenant-1",
                device_id="device-5",
                worker_id="worker-a",
                worker_active_sessions=0,
                worker_max_sessions=5,
                session_epoch="epoch-5",
                now=1_700_000_000,
                expires_at=1_700_000_020,
            )
        with pytest.raises(LeaseConflictError):
            await store.acquire_route(
                tenant_id="tenant-1",
                device_id="device-0",
                worker_id="worker-b",
                worker_active_sessions=0,
                worker_max_sessions=5,
                session_epoch="conflict",
                now=1_700_000_000,
                expires_at=1_700_000_020,
            )

        assert await store.release_route(leases[0]) is True
        replacement = await store.acquire_route(
            tenant_id="tenant-1",
            device_id="device-0",
            worker_id="worker-a",
            worker_active_sessions=0,
            worker_max_sessions=5,
            session_epoch="replacement",
            now=1_700_000_001,
            expires_at=1_700_000_021,
        )
        assert replacement.fencing_token == leases[0].fencing_token + 1
        assert await store.renew_route(
            "worker-a",
            LeaseRenewal(
                tenant_id="tenant-1",
                device_id="device-0",
                session_epoch="replacement",
                fencing_token=replacement.fencing_token,
            ),
            now=1_700_000_010,
            expires_at=1_700_000_030,
        )
        assert not await store.renew_route(
            "worker-a",
            LeaseRenewal(
                tenant_id="tenant-1",
                device_id="device-0",
                session_epoch="stale",
                fencing_token=replacement.fencing_token,
            ),
            now=1_700_000_010,
            expires_at=1_700_000_030,
        )
        claims = ConnectGrantClaims(
            tenant_id="tenant-1",
            device_id="device-0",
            worker_id="worker-a",
            session_epoch="replacement",
            fencing_token=replacement.fencing_token,
            profiles=("wss-opus/1",),
            iat=1_700_000_001,
            exp=1_700_000_020,
            jti="redis-jti-1",
        )
        assert await store.consume_grant(claims, now=1_700_000_010)
        restarted = RedisCoordinationStore(client, prefix=prefix)
        replay = await restarted.consume_grant_result(claims, now=1_700_000_010)
        assert replay.consumed is False
        assert replay.reason == "grant_replay"
    finally:
        keys = [key async for key in client.scan_iter(match=f"{prefix}:*")]
        if keys:
            await client.delete(*keys)
        await store.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_redis_heartbeat_and_drain_interleaving_preserves_drain() -> None:
    url = os.getenv("VOICE_TEST_REDIS_URL")
    if not url:
        pytest.skip("VOICE_TEST_REDIS_URL is not configured")
    client = Redis.from_url(url, decode_responses=False)
    prefix = f"voice-test-{uuid.uuid4().hex}"
    store = RedisCoordinationStore(client, prefix=prefix)
    now = time.time()
    heartbeat = WorkerHeartbeat(
        worker_id="worker-a",
        public_wss_url="ws://worker-a.test/rva/v1/voice",
        active_sessions=0,
        max_sessions=5,
        draining=False,
    )
    try:
        await store.heartbeat(heartbeat, expires_at=now + 30)
        for _ in range(20):
            await __import__("asyncio").gather(
                store.heartbeat(heartbeat, expires_at=now + 30),
                store.set_draining("worker-a", True, now=now),
            )
        workers = await store.list_workers(now=now)
        assert workers[0].draining is True
    finally:
        keys = [key async for key in client.scan_iter(match=f"{prefix}:*")]
        if keys:
            await client.delete(*keys)
        await store.close()
