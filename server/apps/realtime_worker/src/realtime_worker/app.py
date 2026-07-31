from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import math
import secrets
import ssl
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Protocol
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Header, HTTPException, WebSocket, status
from fastapi.responses import JSONResponse
from voice_contracts import BindingAdvertisement, LeaseRenewal, WorkerHeartbeat, WorkerHeartbeatResponse

from .admission import SharedSessionAdmission
from .agent import create_runner
from .auth import AuthContext, VerifiedAuth, WorkerAuthenticator, device_ref, resolve_device_id
from .bindings.rva import RvaRuntimeLimits, RvaWssConnection
from .config import Settings
from .interruption import InterruptionPolicyConfig, LayeredInterruptionPolicy
from .lifecycle import run_with_hard_deadline
from .transport import UdpMediaGateway

logger = logging.getLogger(__name__)

SHUTDOWN_RELEASE_MAX_ATTEMPTS = 32
SHUTDOWN_CLOSE_BUDGET_RATIO = 0.6
SHUTDOWN_INITIAL_HEARTBEAT_MAX_SECONDS = 1.0

LeaseClaimKey = tuple[str, str, str, int]


class LeaseRegistryPort(Protocol):
    def snapshot_active_lease_releases(self) -> None: ...

    async def revoke_expired_leases(self, now: float) -> None: ...

    async def revoke_session_epochs(self, session_epochs: set[str]) -> None: ...

    async def revoke_lease_claims(self, claims: tuple[LeaseRenewal, ...]) -> None: ...

    async def extend_lease_deadlines(
        self,
        expires_at: float,
        accepted_claims: tuple[LeaseRenewal, ...],
    ) -> None: ...

    async def lease_renewal_deadline(self, renewals: tuple[LeaseRenewal, ...]) -> float | None: ...

    def active_lease_renewals(self) -> tuple[LeaseRenewal, ...]: ...

    def pending_lease_releases(self) -> tuple[LeaseRenewal, ...]: ...

    def acknowledge_lease_releases(self, releases: tuple[LeaseRenewal, ...]) -> None: ...


class RvaSessionRegistry:
    """Own RVA connections while sharing process admission with every binding."""

    def __init__(
        self,
        settings: Settings,
        admission: SharedSessionAdmission,
        *,
        udp_gateway: UdpMediaGateway | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._admission = admission
        self._udp_gateway = udp_gateway
        self._connections: dict[tuple[str, str], tuple[RvaWssConnection, AuthContext]] = {}
        self._lease_deadlines: dict[LeaseClaimKey, float] = {}
        self._pending_releases: deque[LeaseRenewal] = deque()
        self._lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._clock = clock
        self._expiry_changed = asyncio.Event()
        self._expiry_task: asyncio.Task[None] | None = None
        self._closing = False
        self._retirement_tasks: dict[int, asyncio.Task[None]] = {}

    async def reserve(self, auth: AuthContext) -> str | None:
        return await self._admission.reserve((auth.tenant_id, auth.device_id))

    async def release_reservation(self, token: str) -> None:
        await self._admission.release(token)

    async def abort_startup(self, auth: AuthContext, token: str) -> None:
        self._queue_lease_release(auth)
        await self._admission.release(token)

    async def run(self, websocket: WebSocket, auth: AuthContext, token: str) -> None:
        principal = (auth.tenant_id, auth.device_id)
        session_epoch = auth.session_epoch or f"lab-{secrets.token_hex(16)}"
        enabled_profiles = {"wss-opus-v3"}
        if self._settings.rva_udp_enabled and self._udp_gateway is not None:
            enabled_profiles.add("udp-opus-gcm-v2")
        try:
            connection = RvaWssConnection(
                websocket,
                expected_device_id=auth.device_id,
                session_id=f"sess-{secrets.token_hex(16)}",
                session_epoch=session_epoch,
                media_id=secrets.token_bytes(8),
                media_epoch=secrets.randbits(32) or 1,
                allowed_profiles=frozenset(enabled_profiles.intersection(auth.allowed_profiles)),
                udp_gateway=self._udp_gateway,
                runner_factory=lambda emit, stop: create_runner(self._settings, emit, stop),
                limits=RvaRuntimeLimits(
                    input_queue_packets=self._settings.rva_input_queue_packets,
                    output_queue_items=self._settings.rva_output_queue_items,
                    max_segment_frames=self._settings.output_segment_max_frames,
                    queue_timeout_seconds=self._settings.rva_queue_timeout_seconds,
                    uplink_max_age_seconds=self._settings.rva_uplink_max_age_seconds,
                    wire_send_timeout_seconds=self._settings.rva_wire_send_timeout_seconds,
                    handshake_timeout_seconds=self._settings.rva_handshake_timeout_seconds,
                    runner_timeout_seconds=self._settings.rva_runner_timeout_seconds,
                    close_timeout_seconds=self._settings.rva_close_timeout_seconds,
                    agent_close_stage_timeout_seconds=self._settings.agent_close_stage_timeout_seconds,
                    playback_prebuffer_packets=self._settings.rva_playback_prebuffer_packets,
                ),
                interruption_policy=_create_interruption_policy(self._settings),
            )
            claim = self._lease_claim(auth)
            async with self._lock:
                self._connections[principal] = (connection, auth)
                if claim is not None and auth.expires_at is not None:
                    self._lease_deadlines[self._lease_claim_key(claim)] = auth.expires_at
                self._expiry_changed.set()
            if claim is not None and auth.expires_at is not None:
                self.start_expiry_enforcement()
        except BaseException:
            await self.abort_startup(auth, token)
            raise
        try:
            await connection.run()
        finally:
            try:
                await connection.wait_closed()
            finally:
                expiry_task: asyncio.Task[None] | None = None
                async with self._lock:
                    current = self._connections.get(principal)
                    if current is not None and current[0] is connection:
                        self._connections.pop(principal, None)
                    claim = self._lease_claim(auth)
                    if claim is not None:
                        self._lease_deadlines.pop(self._lease_claim_key(claim), None)
                    self._queue_lease_release(auth)
                    self._expiry_changed.set()
                    expiry_task = self._detach_expiry_task_if_idle_locked()
                if expiry_task is not None:
                    await asyncio.gather(expiry_task, return_exceptions=True)
                await self._admission.release(token)

    async def close(self) -> None:
        async with self._close_lock:
            self._closing = True
            await self._stop_expiry_enforcement()
            async with self._lock:
                owned = tuple(self._connections.values())
                for _, auth in owned:
                    self._queue_lease_release(auth)
                connections = tuple(connection for connection, _ in owned)
                self._connections.clear()
                self._lease_deadlines.clear()
                self._expiry_changed.set()
            self._schedule_retirements(connections, code=1_001, reason="server_shutdown")
            tasks = tuple(self._retirement_tasks.values())
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            except asyncio.CancelledError:
                tasks = tuple(self._retirement_tasks.values())
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise

    def snapshot_active_lease_releases(self) -> None:
        for _, auth in self._connections.values():
            self._queue_lease_release(auth)

    async def revoke_session_epochs(self, session_epochs: set[str]) -> None:
        claims = tuple(
            claim
            for _, auth in self._connections.values()
            if auth.session_epoch in session_epochs and (claim := self._lease_claim(auth)) is not None
        )
        await self.revoke_lease_claims(claims)

    async def revoke_lease_claims(self, claims: tuple[LeaseRenewal, ...]) -> None:
        claim_keys = {self._lease_claim_key(claim) for claim in claims}
        if not claim_keys:
            return
        async with self._lock:
            connections = self._remove_claims_locked(claim_keys)
            self._expiry_changed.set()
        tasks = self._schedule_retirements(connections, code=1_008, reason="stale_route_lease")
        await self._await_retirements(tasks)

    async def revoke_expired_leases(self, now: float) -> None:
        async with self._lock:
            expired = {claim for claim, deadline in self._lease_deadlines.items() if deadline <= now}
            connections = self._remove_claims_locked(expired)
            self._expiry_changed.set()
        tasks = self._schedule_retirements(connections, code=1_008, reason="stale_route_lease")
        await self._await_retirements(tasks)

    async def extend_lease_deadlines(
        self,
        expires_at: float,
        accepted_claims: tuple[LeaseRenewal, ...],
    ) -> None:
        claim_keys = {self._lease_claim_key(claim) for claim in accepted_claims}
        async with self._lock:
            now = self._clock()
            for claim in claim_keys:
                deadline = self._lease_deadlines.get(claim)
                if deadline is not None and deadline > now:
                    self._lease_deadlines[claim] = expires_at
            self._expiry_changed.set()

    async def lease_renewal_deadline(self, renewals: tuple[LeaseRenewal, ...]) -> float | None:
        claim_keys = {self._lease_claim_key(renewal) for renewal in renewals}
        async with self._lock:
            deadlines = tuple(
                deadline
                for claim, deadline in self._lease_deadlines.items()
                if claim in claim_keys
            )
        return min(deadlines, default=None)

    def active_lease_renewals(self) -> tuple[LeaseRenewal, ...]:
        return tuple(
            LeaseRenewal(
                tenant_id=auth.tenant_id,
                device_id=auth.device_id,
                session_epoch=auth.session_epoch,
                fencing_token=auth.fencing_token,
            )
            for _, auth in self._connections.values()
            if auth.session_epoch is not None and auth.fencing_token is not None
        )

    def pending_lease_releases(self) -> tuple[LeaseRenewal, ...]:
        return tuple(self._pending_releases)

    def acknowledge_lease_releases(self, releases: tuple[LeaseRenewal, ...]) -> None:
        acknowledged = {
            (release.tenant_id, release.device_id, release.session_epoch, release.fencing_token) for release in releases
        }
        self._pending_releases = deque(
            (
                release
                for release in self._pending_releases
                if (release.tenant_id, release.device_id, release.session_epoch, release.fencing_token)
                not in acknowledged
            ),
        )

    @staticmethod
    def _lease_claim(auth: AuthContext) -> LeaseRenewal | None:
        if auth.session_epoch is None or auth.fencing_token is None:
            return None
        return LeaseRenewal(
            tenant_id=auth.tenant_id,
            device_id=auth.device_id,
            session_epoch=auth.session_epoch,
            fencing_token=auth.fencing_token,
        )

    @staticmethod
    def _lease_claim_key(claim: LeaseRenewal) -> LeaseClaimKey:
        return (claim.tenant_id, claim.device_id, claim.session_epoch, claim.fencing_token)

    def _queue_lease_release(self, auth: AuthContext) -> None:
        release = self._lease_claim(auth)
        if release is not None and release not in self._pending_releases:
            self._pending_releases.append(release)

    def start_expiry_enforcement(self) -> None:
        if self._closing:
            return
        task = self._expiry_task
        if task is not None and not task.done():
            return
        task = asyncio.create_task(
            self._enforce_lease_expiry(),
            name="worker-lease-expiry",
        )
        self._expiry_task = task
        task.add_done_callback(self._expiry_enforcement_done)

    async def _stop_expiry_enforcement(self) -> None:
        task, self._expiry_task = self._expiry_task, None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _enforce_lease_expiry(self) -> None:
        while True:
            try:
                async with self._lock:
                    self._expiry_changed.clear()
                    deadline = min(self._lease_deadlines.values(), default=None)
                if deadline is None:
                    await self._expiry_changed.wait()
                    continue
                remaining = deadline - self._clock()
                if remaining <= 0:
                    await self.revoke_expired_leases(self._clock())
                    continue
                try:
                    await asyncio.wait_for(self._expiry_changed.wait(), timeout=remaining)
                except TimeoutError:
                    await self.revoke_expired_leases(self._clock())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.critical(
                    "worker_lease_expiry_enforcement_failed action=fail_closed error_type=%s",
                    type(exc).__name__,
                )
                try:
                    await self.revoke_expired_leases(math.inf)
                except asyncio.CancelledError:
                    raise
                except Exception as fail_closed_exc:
                    self._admission.set_draining(True)
                    logger.critical(
                        "worker_lease_expiry_fail_closed_failed action=terminate_and_drain error_type=%s",
                        type(fail_closed_exc).__name__,
                    )
                    raise

    def _expiry_enforcement_done(self, task: asyncio.Task[None]) -> None:
        if self._expiry_task is task:
            self._expiry_task = None
        if task.cancelled():
            return
        exception = task.exception()
        logger.critical(
            "worker_lease_expiry_enforcement_stopped action=terminated error_type=%s",
            type(exception).__name__ if exception is not None else "none",
        )

    def _detach_expiry_task_if_idle_locked(self) -> asyncio.Task[None] | None:
        if self._lease_deadlines:
            return None
        task, self._expiry_task = self._expiry_task, None
        if task is not None:
            task.cancel()
        return task

    def _remove_claims_locked(
        self,
        claim_keys: set[LeaseClaimKey],
    ) -> tuple[RvaWssConnection, ...]:
        for claim in claim_keys:
            self._lease_deadlines.pop(claim, None)
        connections: list[RvaWssConnection] = []
        for principal, (connection, auth) in tuple(self._connections.items()):
            claim = self._lease_claim(auth)
            if claim is None or self._lease_claim_key(claim) not in claim_keys:
                continue
            current = self._connections.get(principal)
            if current is not None and current[0] is connection:
                self._connections.pop(principal, None)
            self._queue_lease_release(auth)
            connections.append(connection)
        return tuple(connections)

    def _schedule_retirements(
        self,
        connections: tuple[RvaWssConnection, ...],
        *,
        code: int,
        reason: str,
    ) -> tuple[asyncio.Task[None], ...]:
        tasks: list[asyncio.Task[None]] = []
        for connection in connections:
            key = id(connection)
            task = self._retirement_tasks.get(key)
            if task is None:
                task = asyncio.create_task(
                    self._retire_connection(connection, code=code, reason=reason),
                    name=f"rva-registry-retire-{key}",
                )
                self._retirement_tasks[key] = task
                task.add_done_callback(
                    lambda done, key=key, reason=reason: self._retirement_done(key, reason, done)
                )
            tasks.append(task)
        return tuple(tasks)

    async def _retire_connection(
        self,
        connection: RvaWssConnection,
        *,
        code: int,
        reason: str,
    ) -> None:
        try:
            await connection.close(code=code, reason=reason)
        finally:
            await connection.wait_closed()

    async def _await_retirements(self, tasks: tuple[asyncio.Task[None], ...]) -> None:
        await asyncio.gather(
            *(asyncio.shield(task) for task in tasks),
            return_exceptions=True,
        )

    def _retirement_done(
        self,
        key: int,
        reason: str,
        task: asyncio.Task[None],
    ) -> None:
        if self._retirement_tasks.get(key) is task:
            self._retirement_tasks.pop(key, None)
        if task.cancelled():
            if reason == "stale_route_lease":
                logger.critical(
                    "worker_lease_retirement_cancelled close_reason=%s",
                    reason,
                )
            return
        exception = task.exception()
        if exception is None:
            return
        log = logger.critical if reason == "stale_route_lease" else logger.error
        log(
            "worker_lease_retirement_failed close_reason=%s error_type=%s",
            reason,
            type(exception).__name__,
        )


class CombinedLeaseRegistry:
    def __init__(self, *registries: LeaseRegistryPort) -> None:
        self._registries = registries

    async def revoke_expired_leases(self, now: float) -> None:
        await asyncio.gather(*(registry.revoke_expired_leases(now) for registry in self._registries))

    def snapshot_active_lease_releases(self) -> None:
        for registry in self._registries:
            snapshot = getattr(registry, "snapshot_active_lease_releases", None)
            if snapshot is not None:
                snapshot()

    async def revoke_session_epochs(self, session_epochs: set[str]) -> None:
        await asyncio.gather(*(registry.revoke_session_epochs(session_epochs) for registry in self._registries))

    async def revoke_lease_claims(self, claims: tuple[LeaseRenewal, ...]) -> None:
        await asyncio.gather(*(registry.revoke_lease_claims(claims) for registry in self._registries))

    async def extend_lease_deadlines(
        self,
        expires_at: float,
        accepted_claims: tuple[LeaseRenewal, ...],
    ) -> None:
        await asyncio.gather(
            *(registry.extend_lease_deadlines(expires_at, accepted_claims) for registry in self._registries)
        )

    async def lease_renewal_deadline(self, renewals: tuple[LeaseRenewal, ...]) -> float | None:
        deadlines = await asyncio.gather(
            *(registry.lease_renewal_deadline(renewals) for registry in self._registries)
        )
        return min((deadline for deadline in deadlines if deadline is not None), default=None)

    def active_lease_renewals(self) -> tuple[LeaseRenewal, ...]:
        return tuple(renewal for registry in self._registries for renewal in registry.active_lease_renewals())

    def pending_lease_releases(self) -> tuple[LeaseRenewal, ...]:
        releases = tuple(release for registry in self._registries for release in registry.pending_lease_releases())
        return releases[:64]

    def acknowledge_lease_releases(self, releases: tuple[LeaseRenewal, ...]) -> None:
        for registry in self._registries:
            registry.acknowledge_lease_releases(releases)


class ProviderReadiness:
    """Bounded network readiness for routing; session startup still validates provider semantics."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._task: asyncio.Task[None] | None = None
        self.checked = settings.runner != "livekit"
        self.healthy = settings.runner != "livekit"

    async def probe_once(self) -> None:
        if self._settings.runner != "livekit":
            self.checked = True
            self.healthy = True
            return
        endpoints = [
            self._settings.funasr_ws_url,
            self._settings.deepseek_base_url,
            self._tts_endpoint(),
        ]
        results = await asyncio.gather(
            *(
                _probe_endpoint(endpoint, timeout=self._settings.provider_probe_timeout_seconds)
                for endpoint in endpoints
            ),
            return_exceptions=True,
        )
        self.checked = True
        self.healthy = all(result is True for result in results)

    def start(self) -> None:
        if self._settings.runner == "livekit" and self._task is None:
            self._task = asyncio.create_task(self._run(), name="provider-readiness")

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self.probe_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.checked = True
                self.healthy = False
                logger.warning("provider readiness failed error_type=%s", type(exc).__name__)
            await asyncio.sleep(self._settings.provider_probe_interval_seconds)

    def _tts_endpoint(self) -> str:
        if self._settings.tts_provider == "mimo":
            return self._settings.mimo_base_url
        if self._settings.tts_provider == "cosyvoice":
            return self._settings.cosyvoice_url
        return self._settings.remote_cosyvoice_url


async def _probe_endpoint(url: str, *, timeout: float) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https", "ws", "wss"} or parsed.hostname is None:
        return False
    secure = parsed.scheme in {"https", "wss"}
    port = parsed.port or (443 if secure else 80)
    ssl_context = ssl.create_default_context() if secure else None
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(
                parsed.hostname,
                port,
                ssl=ssl_context,
                server_hostname=parsed.hostname if secure else None,
            ),
            timeout=timeout,
        )
    except (OSError, TimeoutError, ssl.SSLError):
        return False
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return True


class DirectorGrantConsumer:
    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(3.0))
        self._owns_client = client is None

    async def consume(self, token: str, *, device_id: str) -> bool:
        if not self._settings.director_url:
            return False
        response = await self._client.post(
            f"{self._settings.director_url.rstrip('/')}/internal/v1/grants/consume",
            headers={"X-Internal-Token": self._settings.internal_token.get_secret_value()},
            json={"token": token, "worker_id": self._settings.worker_id, "device_id": device_id},
        )
        if response.status_code == status.HTTP_200_OK:
            logger.info(
                "director_grant_consume_accepted worker_id=%s device_ref=%s",
                self._settings.worker_id,
                _route_device_ref(self._settings, device_id),
            )
            return True
        logger.warning(
            "director_grant_consume_rejected reason=%s status_code=%d worker_id=%s device_ref=%s token_length=%d",
            _director_reject_reason(response),
            response.status_code,
            self._settings.worker_id,
            _route_device_ref(self._settings, device_id),
            len(token),
        )
        return False

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class WorkerHeartbeatLoop:
    def __init__(
        self,
        settings: Settings,
        admission: SharedSessionAdmission,
        registry: LeaseRegistryPort | None = None,
        provider_readiness: ProviderReadiness | None = None,
        udp_gateway: UdpMediaGateway | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._admission = admission
        self._registry = registry
        self._provider_readiness = provider_readiness or ProviderReadiness(settings)
        self._udp_gateway = udp_gateway
        # Uvicorn and HTTPX both default to a 5 s keep-alive window. The
        # default 5 s heartbeat can otherwise race the server closing an idle
        # connection while the client is reusing it.
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(3.0),
            limits=httpx.Limits(keepalive_expiry=4.0),
        )
        self._owns_client = client is None
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        self.last_success = False

    def start(self) -> None:
        if not self._settings.heartbeat_enabled or not self._settings.director_url or self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="worker-heartbeat")

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._owns_client:
            await self._client.aclose()

    async def send_once(self) -> None:
        async with self._send_lock:
            await self._send_once()

    async def _send_once(self) -> None:
        udp_ready = self._udp_gateway is not None and self._udp_gateway.is_ready
        udp_required = self._settings.rva_udp_enabled
        udp_unavailable = udp_required and not udp_ready
        bindings: list[BindingAdvertisement] = []
        profiles: list[str] = []
        if self._settings.rva_enabled:
            rva_profiles: tuple[str, ...] = ("wss-opus-v3",)
            if self._settings.rva_udp_enabled and udp_ready:
                rva_profiles = ("wss-opus-v3", "udp-opus-gcm-v2")
            bindings.append(
                BindingAdvertisement(
                    control_protocol="rva-control-v2",
                    public_wss_url=self._settings.rva_public_ws_url,
                    profiles=rva_profiles,
                )
            )
            profiles.extend(rva_profiles)
        pending_releases = self._registry.pending_lease_releases() if self._registry is not None else ()
        provider_route_ready = (
            self._provider_readiness.healthy or not self._settings.provider_readiness_required
        )
        payload = WorkerHeartbeat(
            worker_id=self._settings.worker_id,
            public_wss_url=self._settings.rva_public_ws_url,
            active_sessions=self._admission.active_count,
            max_sessions=self._settings.max_sessions,
            draining=self._admission.draining,
            healthy=provider_route_ready and not udp_unavailable,
            profiles=tuple(dict.fromkeys(profiles)),
            bindings=tuple(bindings),
            active_leases=self._registry.active_lease_renewals() if self._registry is not None else (),
            released_leases=pending_releases,
        )
        try:
            renewal_deadline = (
                await self._registry.lease_renewal_deadline(payload.active_leases)
                if self._registry is not None
                else None
            )

            async def exchange_heartbeat() -> tuple[WorkerHeartbeatResponse, tuple[LeaseRenewal, ...]]:
                response = await self._client.post(
                    f"{self._settings.director_url.rstrip('/')}/internal/v1/workers/heartbeat",
                    headers={"X-Internal-Token": self._settings.internal_token.get_secret_value()},
                    json=payload.model_dump(mode="json"),
                )
                response.raise_for_status()
                heartbeat_response = WorkerHeartbeatResponse.model_validate(response.json())
                if (
                    not heartbeat_response.accepted
                    or not math.isfinite(heartbeat_response.heartbeat_expires_at)
                    or not math.isfinite(heartbeat_response.lease_expires_at)
                ):
                    raise ValueError("Director returned an invalid lease renewal response")
                rejected = set(heartbeat_response.rejected_session_epochs)
                rejected_claims = tuple(
                    claim for claim in payload.active_leases if claim.session_epoch in rejected
                )
                accepted_claims = tuple(
                    claim for claim in payload.active_leases if claim.session_epoch not in rejected
                )
                if self._registry is not None:
                    await self._registry.extend_lease_deadlines(
                        heartbeat_response.lease_expires_at,
                        accepted_claims,
                    )
                return heartbeat_response, rejected_claims

            if renewal_deadline is None:
                heartbeat_response, rejected_claims = await exchange_heartbeat()
            else:
                remaining = renewal_deadline - self._clock()
                if remaining <= 0:
                    raise TimeoutError("local route lease expired before renewal")
                async with asyncio.timeout(remaining):
                    heartbeat_response, rejected_claims = await exchange_heartbeat()
        except Exception:
            if self._registry is not None:
                await self._registry.revoke_expired_leases(self._clock())
            raise
        if self._registry is not None:
            await self._registry.revoke_expired_leases(self._clock())
            self._registry.acknowledge_lease_releases(pending_releases)
        if heartbeat_response.draining:
            self._admission.set_draining(True)
        if self._registry is not None:
            rejected = set(heartbeat_response.rejected_session_epochs)
            if rejected:
                logger.warning(
                    "worker_lease_renewal_rejected worker_id=%s active_claims=%d released_claims=%d rejected=%d",
                    self._settings.worker_id,
                    len(payload.active_leases),
                    len(payload.released_leases),
                    len(rejected),
                )
            await self._registry.revoke_lease_claims(rejected_claims)
        self.last_success = True

    async def _run(self) -> None:
        while True:
            try:
                await self.send_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_success = False
                logger.warning("worker heartbeat failed error_type=%s", type(exc).__name__)
            await asyncio.sleep(self._settings.heartbeat_interval_seconds)


def create_app(
    settings: Settings | None = None,
    *,
    grant_consumer: DirectorGrantConsumer | None = None,
) -> FastAPI:
    settings = settings or Settings()
    settings.validate_runtime()
    authenticator = WorkerAuthenticator(settings)
    admission = SharedSessionAdmission(settings.max_sessions)
    udp_gateway = (
        UdpMediaGateway(
            bind_host=settings.udp_bind_host,
            bind_port=settings.udp_bind_port,
            advertised_host=settings.udp_advertise_host,
            advertised_port=settings.udp_advertise_port,
            lifetime_seconds=settings.udp_session_lifetime_seconds,
            probe_timeout_seconds=settings.udp_probe_timeout_seconds,
            queue_size=settings.udp_queue_datagrams,
            reorder_wait_seconds=settings.udp_reorder_wait_ms / 1000,
        )
        if settings.rva_udp_enabled
        else None
    )
    rva_registry = RvaSessionRegistry(settings, admission, udp_gateway=udp_gateway)
    lease_registry = CombinedLeaseRegistry(rva_registry)
    provider_readiness = ProviderReadiness(settings)
    heartbeat = WorkerHeartbeatLoop(
        settings,
        admission,
        lease_registry,
        provider_readiness,
        udp_gateway=udp_gateway,
    )
    active_grant_consumer = grant_consumer or DirectorGrantConsumer(settings)

    async def consume_director_grant(verified: VerifiedAuth) -> bool:
        if verified.director_grant is None:
            return True
        return await active_grant_consumer.consume(
            verified.director_grant,
            device_id=verified.context.device_id,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.admission = admission
        app.state.rva_session_registry = rva_registry
        app.state.udp_media_gateway = udp_gateway
        app.state.worker_heartbeat = heartbeat
        if udp_gateway is not None:
            await udp_gateway.start()
        await provider_readiness.probe_once()
        provider_readiness.start()
        heartbeat.start()
        try:
            yield
        finally:
            admission.set_draining(True)
            lease_registry.snapshot_active_lease_releases()

            async def send_draining_heartbeat() -> bool:
                admission.set_draining(True)
                try:
                    await heartbeat.send_once()
                except Exception:
                    return False
                finally:
                    admission.set_draining(True)
                return True

            async def close_registries() -> None:
                await rva_registry.close()

            async def drain_sessions_and_release_leases() -> None:
                loop = asyncio.get_running_loop()
                started_at = loop.time()
                total_budget = settings.shutdown_drain_timeout_seconds
                deadline = started_at + total_budget
                close_stage_budget = total_budget * SHUTDOWN_CLOSE_BUDGET_RATIO
                initial_release_budget = max(0.0, total_budget - close_stage_budget)
                initial_heartbeat_budget = min(
                    SHUTDOWN_INITIAL_HEARTBEAT_MAX_SECONDS,
                    initial_release_budget,
                    max(0.0, deadline - loop.time()),
                )
                if initial_heartbeat_budget > 0:
                    await run_with_hard_deadline(
                        send_draining_heartbeat(),
                        timeout=initial_heartbeat_budget,
                        task_name="worker-shutdown-initial-heartbeat",
                    )

                close_budget = max(0.0, min(close_stage_budget, deadline - loop.time()))
                if close_budget > 0:
                    closed = await run_with_hard_deadline(
                        close_registries(),
                        timeout=close_budget,
                        task_name="worker-shutdown-registries",
                    )
                    if not closed.completed:
                        logger.warning("worker registry close timed out timeout_seconds=%s", close_budget)

                for _ in range(SHUTDOWN_RELEASE_MAX_ATTEMPTS):
                    if not lease_registry.pending_lease_releases():
                        return
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        break
                    sent = await run_with_hard_deadline(
                        send_draining_heartbeat(),
                        timeout=remaining,
                        task_name="worker-shutdown-final-heartbeat",
                    )
                    if not sent.completed:
                        break
                    if not sent.value:
                        await asyncio.sleep(0)
                if lease_registry.pending_lease_releases():
                    logger.warning(
                        "worker shutdown left lease releases pending after max attempts count=%d",
                        len(lease_registry.pending_lease_releases()),
                    )

            await drain_sessions_and_release_leases()
            await heartbeat.close()
            await provider_readiness.close()
            await active_grant_consumer.close()
            if udp_gateway is not None:
                await udp_gateway.close()

    app = FastAPI(title="Realtime Voice Worker", version="0.1.0", lifespan=lifespan)

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        udp_ready = udp_gateway is None or udp_gateway.is_ready
        required_udp_ready = not settings.rva_udp_enabled or udp_ready
        coordination_ready = not settings.director_url or heartbeat.last_success
        provider_route_ready = provider_readiness.healthy or not settings.provider_readiness_required
        ready_value = not admission.draining and required_udp_ready and provider_route_ready and coordination_ready
        return JSONResponse(
            status_code=status.HTTP_200_OK if ready_value else status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "ready" if ready_value else "not_ready",
                "worker_id": settings.worker_id,
                "active_sessions": admission.active_count,
                "max_sessions": settings.max_sessions,
                "draining": admission.draining,
                "runner": settings.runner,
                "provider_network_checked": provider_readiness.checked,
                "provider_network_ready": provider_readiness.healthy,
                "provider_network_required": settings.provider_readiness_required,
                "coordination_ready": coordination_ready,
                "rva_enabled": settings.rva_enabled,
                "rva_udp_enabled": settings.rva_udp_enabled,
                "rva_udp_ready": udp_ready if settings.rva_udp_enabled else False,
            },
        )

    @app.post("/internal/v1/drain")
    async def drain(
        x_internal_token: str | None = Header(default=None),
    ) -> dict[str, bool]:
        if x_internal_token is None or not hmac.compare_digest(
            x_internal_token, settings.internal_token.get_secret_value()
        ):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_internal_credentials")
        admission.set_draining(True)
        with contextlib.suppress(Exception):
            await heartbeat.send_once()
        return {"draining": True}

    if settings.rva_enabled:

        @app.websocket("/v2/voice")
        async def rva_voice(websocket: WebSocket) -> None:
            device_id = resolve_device_id(websocket.headers.get("device-id"), websocket.headers.get("client-id"))
            verified = authenticator.verify(
                websocket.headers.get("authorization"),
                device_id,
                control_protocol="rva-control-v2",
            )
            enabled_profiles = {"wss-opus-v3"}
            if settings.rva_udp_enabled:
                enabled_profiles.add("udp-opus-gcm-v2")
            if verified is None:
                _log_websocket_rejected(
                    settings,
                    binding="rva-control-v2",
                    reason="invalid_credentials",
                    device_id=device_id,
                )
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="invalid_credentials")
                return
            if not enabled_profiles.intersection(verified.context.allowed_profiles):
                _log_websocket_rejected(
                    settings,
                    binding="rva-control-v2",
                    reason="no_compatible_profile",
                    auth=verified.context,
                )
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="invalid_credentials")
                return
            reservation = await rva_registry.reserve(verified.context)
            if reservation is None:
                _log_websocket_rejected(
                    settings,
                    binding="rva-control-v2",
                    reason="session_overloaded",
                    auth=verified.context,
                )
                await websocket.close(code=1_013, reason="session_overloaded")
                return
            try:
                grant_consumed = await consume_director_grant(verified)
            except BaseException:
                await rva_registry.abort_startup(verified.context, reservation)
                raise
            if not grant_consumed:
                await rva_registry.release_reservation(reservation)
                _log_websocket_rejected(
                    settings,
                    binding="rva-control-v2",
                    reason="grant_rejected",
                    auth=verified.context,
                )
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="grant_rejected")
                return
            try:
                await websocket.accept()
            except BaseException:
                _log_websocket_rejected(
                    settings,
                    binding="rva-control-v2",
                    reason="accept_failed",
                    auth=verified.context,
                )
                await rva_registry.abort_startup(verified.context, reservation)
                raise
            _log_websocket_accepted(settings, binding="rva-control-v2", auth=verified.context)
            await rva_registry.run(websocket, verified.context, reservation)

    return app


def _create_interruption_policy(settings: Settings) -> LayeredInterruptionPolicy:
    return LayeredInterruptionPolicy(
        InterruptionPolicyConfig(enabled=settings.interruption_policy_enabled)
    )


def _safe_reason(value: object) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        return None
    if all(character.islower() or character.isdigit() or character == "_" for character in value):
        return value
    return None


def _director_reject_reason(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        body = None
    reason: object = None
    if isinstance(body, dict):
        reason = body.get("reason")
        detail = body.get("detail")
        if reason is None and isinstance(detail, dict):
            reason = detail.get("reason")
        elif reason is None:
            reason = detail
    return _safe_reason(reason) or f"http_{response.status_code}"


def _route_device_ref(settings: Settings, device_id: str | None) -> str:
    if device_id is None:
        return "missing"
    return device_ref(
        f"worker:{settings.worker_id}",
        device_id,
        settings.internal_token.get_secret_value(),
    )


def _auth_device_ref(settings: Settings, auth: AuthContext) -> str:
    return device_ref(auth.tenant_id, auth.device_id, settings.internal_token.get_secret_value())


def _profiles_for_log(profiles: tuple[str, ...]) -> str:
    return ",".join(profiles) or "none"


def _log_websocket_rejected(
    settings: Settings,
    *,
    binding: str,
    reason: str,
    auth: AuthContext | None = None,
    device_id: str | None = None,
) -> None:
    session_epoch = auth.session_epoch if auth is not None and auth.session_epoch is not None else "none"
    profiles = _profiles_for_log(tuple(auth.allowed_profiles)) if auth is not None else "unknown"
    logger.warning(
        "worker_websocket_rejected binding=%s reason=%s worker_id=%s device_ref=%s session_epoch=%s profiles=%s",
        binding,
        reason,
        settings.worker_id,
        _auth_device_ref(settings, auth) if auth is not None else _route_device_ref(settings, device_id),
        session_epoch,
        profiles,
    )


def _log_websocket_accepted(settings: Settings, *, binding: str, auth: AuthContext) -> None:
    logger.info(
        "worker_websocket_accepted binding=%s worker_id=%s device_ref=%s session_epoch=%s profiles=%s",
        binding,
        settings.worker_id,
        _auth_device_ref(settings, auth),
        auth.session_epoch or "none",
        _profiles_for_log(tuple(auth.allowed_profiles)),
    )
