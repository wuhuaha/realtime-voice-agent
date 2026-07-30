from __future__ import annotations

import logging
import secrets
import time
import uuid
from collections.abc import Callable

from voice_contracts import (
    BootstrapRequest,
    BootstrapResponse,
    ConnectGrantClaims,
    GrantCodec,
    GrantError,
    LeaseRenewal,
    RouteReleaseRequest,
    WorkerHeartbeat,
    WorkerHeartbeatResponse,
    WorkerSnapshot,
)

from .store import CoordinationStorePort, WorkerCapacityError

logger = logging.getLogger(__name__)


class NoCapacityError(RuntimeError):
    pass


class GrantConsumeError(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class DirectorService:
    def __init__(
        self,
        store: CoordinationStorePort,
        grant_codec: GrantCodec,
        *,
        heartbeat_ttl_seconds: float,
        lease_ttl_seconds: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._grant_codec = grant_codec
        self._heartbeat_ttl_seconds = heartbeat_ttl_seconds
        self._lease_ttl_seconds = lease_ttl_seconds
        self._clock = clock

    async def heartbeat(self, heartbeat: WorkerHeartbeat) -> WorkerHeartbeatResponse:
        now = self._clock()
        snapshot = await self._store.heartbeat(heartbeat, expires_at=now + self._heartbeat_ttl_seconds)
        release_matches = 0
        for release in heartbeat.released_leases:
            release_matches += int(await self._store.release_route_claim(heartbeat.worker_id, release))
        rejected: list[str] = []
        renewal_matches = 0
        for renewal in heartbeat.active_leases:
            accepted = await self._store.renew_route(
                heartbeat.worker_id,
                renewal,
                now=now,
                expires_at=now + self._lease_ttl_seconds,
            )
            if not accepted:
                rejected.append(renewal.session_epoch)
            else:
                renewal_matches += 1
        if heartbeat.released_leases or heartbeat.active_leases:
            released_routes = {(item.tenant_id, item.device_id) for item in heartbeat.released_leases}
            active_routes = {(item.tenant_id, item.device_id) for item in heartbeat.active_leases}
            released_claims = {
                (item.tenant_id, item.device_id, item.session_epoch, item.fencing_token)
                for item in heartbeat.released_leases
            }
            active_claims = {
                (item.tenant_id, item.device_id, item.session_epoch, item.fencing_token)
                for item in heartbeat.active_leases
            }
            if rejected:
                log = logger.warning
            elif released_routes.intersection(active_routes):
                log = logger.info
            else:
                log = logger.debug
            log(
                "director_lease_heartbeat worker_id=%s release_claims=%d release_matches=%d "
                "active_claims=%d renewal_matches=%d renewal_rejected=%d route_overlap=%d exact_overlap=%d",
                heartbeat.worker_id,
                len(heartbeat.released_leases),
                release_matches,
                len(heartbeat.active_leases),
                renewal_matches,
                len(rejected),
                len(released_routes.intersection(active_routes)),
                len(released_claims.intersection(active_claims)),
            )
        return WorkerHeartbeatResponse(
            draining=snapshot.draining,
            heartbeat_expires_at=snapshot.heartbeat_expires_at,
            lease_expires_at=now + self._lease_ttl_seconds,
            rejected_session_epochs=tuple(rejected),
        )

    async def consume_grant(self, token: str, *, worker_id: str, device_id: str) -> ConnectGrantClaims:
        try:
            claims = self._grant_codec.verify(token, worker_id=worker_id, device_id=device_id)
        except GrantError as exc:
            raise GrantConsumeError(_grant_error_reason(exc)) from exc
        result = await self._store.consume_grant_result(claims, now=self._clock())
        if not result.consumed:
            raise GrantConsumeError(result.reason)
        return claims

    async def bootstrap(self, request: BootstrapRequest) -> BootstrapResponse:
        now = self._clock()
        workers = await self._store.list_workers(now=now)
        expires_at = now + self._lease_ttl_seconds
        candidates = self._candidate_workers(
            workers,
            request.supported_profiles,
            request.control_protocol,
        )
        lease = None
        selected: tuple[WorkerSnapshot, str, tuple[str, ...]] | None = None
        for worker, public_wss_url, profiles in candidates:
            try:
                lease = await self._store.acquire_route(
                    tenant_id=request.tenant_id,
                    device_id=request.device_id,
                    worker_id=worker.worker_id,
                    worker_active_sessions=worker.active_sessions,
                    worker_max_sessions=worker.max_sessions,
                    session_epoch=f"epoch-{uuid.uuid4().hex}",
                    now=now,
                    expires_at=expires_at,
                )
            except WorkerCapacityError:
                continue
            selected = (worker, public_wss_url, profiles)
            break
        if lease is None or selected is None:
            raise NoCapacityError("all matching workers reached their admission limit")
        worker, public_wss_url, profiles = selected
        claims = ConnectGrantClaims(
            tenant_id=request.tenant_id,
            device_id=request.device_id,
            worker_id=worker.worker_id,
            session_epoch=lease.session_epoch,
            fencing_token=lease.fencing_token,
            profiles=profiles,
            control_protocol=request.control_protocol,
            iat=now,
            exp=expires_at,
            jti=f"jti-{secrets.token_hex(16)}",
        )
        return BootstrapResponse(
            worker_id=worker.worker_id,
            worker_wss_url=public_wss_url,
            connect_grant=self._grant_codec.issue(claims),
            session_epoch=lease.session_epoch,
            fencing_token=lease.fencing_token,
            allowed_profiles=profiles,
            control_protocol=request.control_protocol,
            expires_at=expires_at,
        )

    async def release(self, request: RouteReleaseRequest) -> bool:
        return await self._store.release_route_claim(
            request.worker_id,
            LeaseRenewal(
                tenant_id=request.tenant_id,
                device_id=request.device_id,
                session_epoch=request.session_epoch,
                fencing_token=request.fencing_token,
            ),
        )

    async def set_draining(self, worker_id: str, draining: bool) -> WorkerSnapshot:
        return await self._store.set_draining(worker_id, draining, now=self._clock())

    @staticmethod
    def _candidate_workers(
        workers: tuple[WorkerSnapshot, ...], requested_profiles: tuple[str, ...],
        control_protocol: str,
    ) -> tuple[tuple[WorkerSnapshot, str, tuple[str, ...]], ...]:
        candidates: list[tuple[WorkerSnapshot, str, tuple[str, ...]]] = []
        for worker in workers:
            binding = next(
                (item for item in worker.resolved_bindings() if item.control_protocol == control_protocol),
                None,
            )
            if binding is None:
                continue
            allowed = tuple(profile for profile in requested_profiles if profile in binding.profiles)
            if worker.healthy and not worker.draining and worker.available_slots > 0 and allowed:
                candidates.append((worker, binding.public_wss_url, allowed))
        if not candidates:
            raise NoCapacityError("no ready worker supports the requested profiles")
        return tuple(
            sorted(
                candidates,
                key=lambda item: (
                    item[0].active_sessions / item[0].max_sessions,
                    item[0].active_sessions,
                    item[0].worker_id,
                ),
            )
        )


def _grant_error_reason(error: GrantError) -> str:
    reasons = {
        "malformed grant": "malformed_grant",
        "malformed grant encoding": "malformed_grant_encoding",
        "malformed grant header": "malformed_grant_header",
        "unsupported grant header": "unsupported_grant_header",
        "invalid grant signature": "invalid_grant_signature",
        "invalid grant claims": "invalid_grant_claims",
        "grant expired": "grant_expired",
        "invalid grant lifetime": "invalid_grant_lifetime",
        "grant belongs to another worker": "worker_mismatch",
        "grant belongs to another device": "device_mismatch",
    }
    return reasons.get(str(error), "invalid_grant")
