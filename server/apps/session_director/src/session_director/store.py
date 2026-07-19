from __future__ import annotations

import asyncio
import json
import time
from typing import Protocol

from redis.asyncio import Redis
from voice_contracts import (
    ConnectGrantClaims,
    LeaseRenewal,
    RouteLease,
    WorkerHeartbeat,
    WorkerSnapshot,
    encode_route_key,
)


class LeaseConflictError(RuntimeError):
    pass


class WorkerNotFoundError(LookupError):
    pass


class WorkerCapacityError(RuntimeError):
    pass


class CoordinationStorePort(Protocol):
    async def heartbeat(self, heartbeat: WorkerHeartbeat, *, expires_at: float) -> WorkerSnapshot: ...

    async def list_workers(self, *, now: float) -> tuple[WorkerSnapshot, ...]: ...

    async def set_draining(self, worker_id: str, draining: bool, *, now: float) -> WorkerSnapshot: ...

    async def acquire_route(
        self,
        *,
        tenant_id: str,
        device_id: str,
        worker_id: str,
        worker_active_sessions: int,
        worker_max_sessions: int,
        session_epoch: str,
        now: float,
        expires_at: float,
    ) -> RouteLease: ...

    async def release_route(self, lease: RouteLease) -> bool: ...

    async def renew_route(
        self,
        worker_id: str,
        renewal: LeaseRenewal,
        *,
        now: float,
        expires_at: float,
    ) -> bool: ...

    async def release_route_claim(self, worker_id: str, release: LeaseRenewal) -> bool: ...

    async def consume_grant(self, claims: ConnectGrantClaims, *, now: float) -> bool: ...

    async def ping(self) -> bool: ...

    async def close(self) -> None: ...


class InMemoryCoordinationStore:
    """Atomic process-local adapter for tests and single-process development only."""

    def __init__(self) -> None:
        self._workers: dict[str, WorkerSnapshot] = {}
        self._leases: dict[str, RouteLease] = {}
        self._fencing: dict[str, int] = {}
        self._consumed_jti: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def heartbeat(self, heartbeat: WorkerHeartbeat, *, expires_at: float) -> WorkerSnapshot:
        snapshot = WorkerSnapshot(**heartbeat.model_dump(), heartbeat_expires_at=expires_at)
        async with self._lock:
            current = self._workers.get(heartbeat.worker_id)
            if current is not None and current.draining:
                snapshot = snapshot.model_copy(update={"draining": True})
            self._workers[heartbeat.worker_id] = snapshot
        return snapshot

    async def list_workers(self, *, now: float) -> tuple[WorkerSnapshot, ...]:
        async with self._lock:
            expired = [worker_id for worker_id, worker in self._workers.items() if worker.heartbeat_expires_at <= now]
            for worker_id in expired:
                self._workers.pop(worker_id, None)
            return tuple(self._workers.values())

    async def set_draining(self, worker_id: str, draining: bool, *, now: float) -> WorkerSnapshot:
        async with self._lock:
            worker = self._workers.get(worker_id)
            if worker is None or worker.heartbeat_expires_at <= now:
                raise WorkerNotFoundError(worker_id)
            updated = worker.model_copy(update={"draining": draining})
            self._workers[worker_id] = updated
            return updated

    async def acquire_route(
        self,
        *,
        tenant_id: str,
        device_id: str,
        worker_id: str,
        worker_active_sessions: int,
        worker_max_sessions: int,
        session_epoch: str,
        now: float,
        expires_at: float,
    ) -> RouteLease:
        route_key = encode_route_key(tenant_id, device_id)
        async with self._lock:
            expired = [key for key, lease in self._leases.items() if lease.expires_at <= now]
            for key in expired:
                self._leases.pop(key, None)
            current = self._leases.get(route_key)
            if current is not None and current.expires_at > now:
                raise LeaseConflictError(route_key)
            reserved = sum(lease.worker_id == worker_id for lease in self._leases.values())
            if max(worker_active_sessions, reserved) >= worker_max_sessions:
                raise WorkerCapacityError(worker_id)
            fencing_token = self._fencing.get(route_key, 0) + 1
            self._fencing[route_key] = fencing_token
            lease = RouteLease(
                tenant_id=tenant_id,
                device_id=device_id,
                worker_id=worker_id,
                session_epoch=session_epoch,
                fencing_token=fencing_token,
                expires_at=expires_at,
            )
            self._leases[route_key] = lease
            return lease

    async def release_route(self, lease: RouteLease) -> bool:
        async with self._lock:
            current = self._leases.get(lease.route_key)
            if current != lease:
                return False
            self._leases.pop(lease.route_key, None)
            return True

    async def renew_route(
        self,
        worker_id: str,
        renewal: LeaseRenewal,
        *,
        now: float,
        expires_at: float,
    ) -> bool:
        route_key = encode_route_key(renewal.tenant_id, renewal.device_id)
        async with self._lock:
            current = self._leases.get(route_key)
            if (
                current is None
                or current.expires_at <= now
                or current.worker_id != worker_id
                or current.session_epoch != renewal.session_epoch
                or current.fencing_token != renewal.fencing_token
            ):
                return False
            self._leases[route_key] = current.model_copy(update={"expires_at": expires_at})
            return True

    async def release_route_claim(self, worker_id: str, release: LeaseRenewal) -> bool:
        route_key = encode_route_key(release.tenant_id, release.device_id)
        async with self._lock:
            current = self._leases.get(route_key)
            if (
                current is None
                or current.worker_id != worker_id
                or current.session_epoch != release.session_epoch
                or current.fencing_token != release.fencing_token
            ):
                return False
            self._leases.pop(route_key, None)
            return True

    async def consume_grant(self, claims: ConnectGrantClaims, *, now: float) -> bool:
        route_key = encode_route_key(claims.tenant_id, claims.device_id)
        async with self._lock:
            self._consumed_jti = {jti: expiry for jti, expiry in self._consumed_jti.items() if expiry > now}
            current = self._leases.get(route_key)
            if (
                current is None
                or current.expires_at <= now
                or claims.exp <= now
                or current.worker_id != claims.worker_id
                or current.session_epoch != claims.session_epoch
                or current.fencing_token != claims.fencing_token
                or claims.jti in self._consumed_jti
            ):
                return False
            self._consumed_jti[claims.jti] = claims.exp
            return True

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        return None


_ACQUIRE_ROUTE_LUA = """
local current = redis.call('GET', KEYS[1])
if current then
  local decoded = cjson.decode(current)
  if tonumber(decoded['expires_at']) > tonumber(ARGV[1]) then
    return {0, tonumber(decoded['fencing_token'])}
  end
  redis.call('DEL', KEYS[1])
end
redis.call('ZREMRANGEBYSCORE', KEYS[3], '-inf', ARGV[1])
local reserved = redis.call('ZCARD', KEYS[3])
if math.max(tonumber(ARGV[4]), reserved) >= tonumber(ARGV[5]) then
  return {-1, 0}
end
local fence = redis.call('INCR', KEYS[2])
local lease = cjson.decode(ARGV[2])
lease['fencing_token'] = fence
local encoded = cjson.encode(lease)
redis.call('SET', KEYS[1], encoded, 'PX', ARGV[3])
redis.call('ZADD', KEYS[3], ARGV[6], KEYS[1])
return {1, fence}
"""


_RELEASE_ROUTE_LUA = """
local current = redis.call('GET', KEYS[1])
if not current then return 0 end
local decoded = cjson.decode(current)
if decoded['worker_id'] ~= ARGV[1] or decoded['session_epoch'] ~= ARGV[2]
   or tonumber(decoded['fencing_token']) ~= tonumber(ARGV[3]) then
  return 0
end
redis.call('DEL', KEYS[1])
redis.call('ZREM', KEYS[2], KEYS[1])
return 1
"""


_RENEW_ROUTE_LUA = """
local current = redis.call('GET', KEYS[1])
if not current then return 0 end
local decoded = cjson.decode(current)
if tonumber(decoded['expires_at']) <= tonumber(ARGV[1])
   or decoded['worker_id'] ~= ARGV[2]
   or decoded['session_epoch'] ~= ARGV[3]
   or tonumber(decoded['fencing_token']) ~= tonumber(ARGV[4]) then
  return 0
end
decoded['expires_at'] = tonumber(ARGV[5])
redis.call('SET', KEYS[1], cjson.encode(decoded), 'PX', ARGV[6])
redis.call('ZADD', KEYS[2], ARGV[5], KEYS[1])
return 1
"""


_HEARTBEAT_LUA = """
local draining = ARGV[3]
local current_drain = redis.call('GET', KEYS[2])
if current_drain == '1' or draining == '1' then draining = '1' end
redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2])
redis.call('SET', KEYS[2], draining, 'PX', ARGV[2])
return draining
"""


_SET_DRAINING_LUA = """
local current = redis.call('GET', KEYS[1])
if not current then return nil end
local ttl = redis.call('PTTL', KEYS[1])
if ttl <= 0 then return nil end
local decoded = cjson.decode(current)
if tonumber(decoded['heartbeat_expires_at']) <= tonumber(ARGV[1]) then return nil end
redis.call('SET', KEYS[2], ARGV[2], 'PX', ttl)
return current
"""


_CONSUME_GRANT_LUA = """
local current = redis.call('GET', KEYS[1])
if not current then return 0 end
local route = cjson.decode(current)
if tonumber(route['expires_at']) <= tonumber(ARGV[1])
   or tonumber(ARGV[7]) <= tonumber(ARGV[1])
   or route['tenant_id'] ~= ARGV[2]
   or route['device_id'] ~= ARGV[3]
   or route['worker_id'] ~= ARGV[4]
   or route['session_epoch'] ~= ARGV[5]
   or tonumber(route['fencing_token']) ~= tonumber(ARGV[6]) then
  return 0
end
local consumed = redis.call('SET', KEYS[2], '1', 'NX', 'PX', ARGV[8])
if not consumed then return 0 end
return 1
"""


class RedisCoordinationStore:
    def __init__(self, redis: Redis, *, prefix: str) -> None:
        self._redis = redis
        self._prefix = prefix.rstrip(":")

    def _worker_key(self, worker_id: str) -> str:
        return f"{self._prefix}:worker:{worker_id}"

    def _worker_drain_key(self, worker_id: str) -> str:
        return f"{self._prefix}:worker-drain:{worker_id}"

    def _route_key(self, tenant_id: str, device_id: str) -> str:
        return f"{self._prefix}:route:{encode_route_key(tenant_id, device_id)}"

    def _fence_key(self, tenant_id: str, device_id: str) -> str:
        return f"{self._prefix}:fence:{encode_route_key(tenant_id, device_id)}"

    def _worker_routes_key(self, worker_id: str) -> str:
        return f"{self._prefix}:worker-routes:{worker_id}"

    def _grant_jti_key(self, jti: str) -> str:
        return f"{self._prefix}:grant-jti:{jti}"

    async def heartbeat(self, heartbeat: WorkerHeartbeat, *, expires_at: float) -> WorkerSnapshot:
        snapshot = WorkerSnapshot(
            **heartbeat.model_dump(),
            heartbeat_expires_at=expires_at,
        )
        ttl_ms = max(1, round((expires_at - time.time()) * 1000))
        draining = await self._redis.eval(
            _HEARTBEAT_LUA,
            2,
            self._worker_key(heartbeat.worker_id),
            self._worker_drain_key(heartbeat.worker_id),
            snapshot.model_dump_json(),
            str(ttl_ms),
            "1" if heartbeat.draining else "0",
        )
        return snapshot.model_copy(update={"draining": draining == b"1" or draining == "1"})

    async def list_workers(self, *, now: float) -> tuple[WorkerSnapshot, ...]:
        workers: list[WorkerSnapshot] = []
        async for key in self._redis.scan_iter(match=f"{self._prefix}:worker:*", count=100):
            raw = await self._redis.get(key)
            if not raw:
                continue
            try:
                worker = WorkerSnapshot.model_validate_json(raw)
            except ValueError:
                continue
            if worker.heartbeat_expires_at > now:
                draining = await self._redis.get(self._worker_drain_key(worker.worker_id))
                worker = worker.model_copy(update={"draining": draining == b"1" or draining == "1"})
                workers.append(worker)
        return tuple(workers)

    async def set_draining(self, worker_id: str, draining: bool, *, now: float) -> WorkerSnapshot:
        encoded = await self._redis.eval(
            _SET_DRAINING_LUA,
            2,
            self._worker_key(worker_id),
            self._worker_drain_key(worker_id),
            str(now),
            "1" if draining else "0",
        )
        if not encoded:
            raise WorkerNotFoundError(worker_id)
        return WorkerSnapshot.model_validate_json(encoded).model_copy(update={"draining": draining})

    async def acquire_route(
        self,
        *,
        tenant_id: str,
        device_id: str,
        worker_id: str,
        worker_active_sessions: int,
        worker_max_sessions: int,
        session_epoch: str,
        now: float,
        expires_at: float,
    ) -> RouteLease:
        lease_without_fence = {
            "tenant_id": tenant_id,
            "device_id": device_id,
            "worker_id": worker_id,
            "session_epoch": session_epoch,
            "fencing_token": 1,
            "expires_at": expires_at,
        }
        ttl_ms = max(1, round((expires_at - now) * 1000))
        acquired, fencing_token = await self._redis.eval(
            _ACQUIRE_ROUTE_LUA,
            3,
            self._route_key(tenant_id, device_id),
            self._fence_key(tenant_id, device_id),
            self._worker_routes_key(worker_id),
            str(now),
            json.dumps(lease_without_fence, separators=(",", ":")),
            str(ttl_ms),
            str(worker_active_sessions),
            str(worker_max_sessions),
            str(expires_at),
        )
        if int(acquired) == -1:
            raise WorkerCapacityError(worker_id)
        if int(acquired) != 1:
            raise LeaseConflictError(f"{tenant_id}:{device_id}")
        lease_without_fence["fencing_token"] = int(fencing_token)
        return RouteLease(**lease_without_fence)

    async def release_route(self, lease: RouteLease) -> bool:
        released = await self._redis.eval(
            _RELEASE_ROUTE_LUA,
            2,
            self._route_key(lease.tenant_id, lease.device_id),
            self._worker_routes_key(lease.worker_id),
            lease.worker_id,
            lease.session_epoch,
            str(lease.fencing_token),
        )
        return bool(released)

    async def renew_route(
        self,
        worker_id: str,
        renewal: LeaseRenewal,
        *,
        now: float,
        expires_at: float,
    ) -> bool:
        ttl_ms = max(1, round((expires_at - now) * 1000))
        renewed = await self._redis.eval(
            _RENEW_ROUTE_LUA,
            2,
            self._route_key(renewal.tenant_id, renewal.device_id),
            self._worker_routes_key(worker_id),
            str(now),
            worker_id,
            renewal.session_epoch,
            str(renewal.fencing_token),
            str(expires_at),
            str(ttl_ms),
        )
        return bool(renewed)

    async def release_route_claim(self, worker_id: str, release: LeaseRenewal) -> bool:
        released = await self._redis.eval(
            _RELEASE_ROUTE_LUA,
            2,
            self._route_key(release.tenant_id, release.device_id),
            self._worker_routes_key(worker_id),
            worker_id,
            release.session_epoch,
            str(release.fencing_token),
        )
        return bool(released)

    async def consume_grant(self, claims: ConnectGrantClaims, *, now: float) -> bool:
        ttl_ms = max(1, round((claims.exp - now) * 1000))
        consumed = await self._redis.eval(
            _CONSUME_GRANT_LUA,
            2,
            self._route_key(claims.tenant_id, claims.device_id),
            self._grant_jti_key(claims.jti),
            str(now),
            claims.tenant_id,
            claims.device_id,
            claims.worker_id,
            claims.session_epoch,
            str(claims.fencing_token),
            str(claims.exp),
            str(ttl_ms),
        )
        return bool(consumed)

    async def ping(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except Exception:
            return False

    async def close(self) -> None:
        await self._redis.aclose()
