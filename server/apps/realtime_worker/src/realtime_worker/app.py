from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
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
from voice_contracts import BindingAdvertisement, LeaseRenewal, WorkerHeartbeat

from .agent import create_runner
from .auth import AuthContext, VerifiedAuth, WorkerAuthenticator, resolve_device_id
from .bindings.rva import RvaRuntimeLimits, RvaWssConnection
from .bindings.xiaozhi import SharedSessionAdmission, XiaozhiSessionRegistry, resolve_xiaozhi_device_id
from .config import Settings
from .transport import UdpMediaGateway

logger = logging.getLogger(__name__)


class LeaseRegistryPort(Protocol):
    async def revoke_expired_leases(self, now: float) -> None: ...

    async def revoke_session_epochs(self, session_epochs: set[str]) -> None: ...

    def extend_lease_deadlines(self, expires_at: float, rejected_epochs: set[str]) -> None: ...

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
    ) -> None:
        self._settings = settings
        self._admission = admission
        self._udp_gateway = udp_gateway
        self._connections: dict[tuple[str, str], tuple[RvaWssConnection, AuthContext]] = {}
        self._lease_deadlines: dict[str, float] = {}
        self._pending_releases: deque[LeaseRenewal] = deque()
        self._lock = asyncio.Lock()

    async def reserve(self, auth: AuthContext) -> str | None:
        return await self._admission.reserve((auth.tenant_id, auth.device_id))

    async def release_reservation(self, token: str) -> None:
        await self._admission.release(token)

    async def run(self, websocket: WebSocket, auth: AuthContext, token: str) -> None:
        principal = (auth.tenant_id, auth.device_id)
        session_epoch = auth.session_epoch or f"lab-{secrets.token_hex(16)}"
        enabled_profiles = {"wss-opus-v2"}
        if self._settings.rva_udp_enabled and self._udp_gateway is not None:
            enabled_profiles.add("udp-opus-gcm-v1")
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
                    handshake_timeout_seconds=self._settings.rva_handshake_timeout_seconds,
                    runner_timeout_seconds=self._settings.rva_runner_timeout_seconds,
                    close_timeout_seconds=self._settings.rva_close_timeout_seconds,
                    playback_prebuffer_packets=self._settings.rva_playback_prebuffer_packets,
                ),
            )
        except Exception:
            await self._admission.release(token)
            raise
        async with self._lock:
            self._connections[principal] = (connection, auth)
            if auth.session_epoch is not None and auth.expires_at is not None:
                self._lease_deadlines[auth.session_epoch] = auth.expires_at
        try:
            await connection.run()
        finally:
            try:
                await connection.wait_closed()
            finally:
                async with self._lock:
                    current = self._connections.get(principal)
                    if current is not None and current[0] is connection:
                        self._connections.pop(principal, None)
                    if auth.session_epoch is not None:
                        self._lease_deadlines.pop(auth.session_epoch, None)
                    release = self._lease_claim(auth)
                    if release is not None:
                        self._pending_releases.append(release)
                await self._admission.release(token)

    async def close(self) -> None:
        async with self._lock:
            connections = tuple(connection for connection, _ in self._connections.values())
            self._connections.clear()
        await asyncio.gather(
            *(connection.close(code=1_001, reason="server_shutdown") for connection in connections),
            return_exceptions=True,
        )
        await asyncio.gather(*(connection.wait_closed() for connection in connections), return_exceptions=True)

    async def revoke_session_epochs(self, session_epochs: set[str]) -> None:
        if not session_epochs:
            return
        async with self._lock:
            connections = tuple(
                connection
                for connection, auth in self._connections.values()
                if auth.session_epoch in session_epochs
            )
        await asyncio.gather(
            *(connection.close(code=1_008, reason="stale_route_lease") for connection in connections),
            return_exceptions=True,
        )
        await asyncio.gather(*(connection.wait_closed() for connection in connections), return_exceptions=True)

    async def revoke_expired_leases(self, now: float) -> None:
        expired = {epoch for epoch, deadline in self._lease_deadlines.items() if deadline <= now}
        await self.revoke_session_epochs(expired)

    def extend_lease_deadlines(self, expires_at: float, rejected_epochs: set[str]) -> None:
        for session_epoch in tuple(self._lease_deadlines):
            if session_epoch not in rejected_epochs:
                self._lease_deadlines[session_epoch] = expires_at

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


class CombinedLeaseRegistry:
    def __init__(self, *registries: LeaseRegistryPort) -> None:
        self._registries = registries

    async def revoke_expired_leases(self, now: float) -> None:
        await asyncio.gather(*(registry.revoke_expired_leases(now) for registry in self._registries))

    async def revoke_session_epochs(self, session_epochs: set[str]) -> None:
        await asyncio.gather(*(registry.revoke_session_epochs(session_epochs) for registry in self._registries))

    def extend_lease_deadlines(self, expires_at: float, rejected_epochs: set[str]) -> None:
        for registry in self._registries:
            registry.extend_lease_deadlines(expires_at, rejected_epochs)

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
        return response.status_code == status.HTTP_200_OK

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
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(3.0))
        self._owns_client = client is None
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
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
        if self._registry is not None:
            await self._registry.revoke_expired_leases(self._clock())
        udp_ready = self._udp_gateway is not None and self._udp_gateway.is_ready
        udp_unavailable = (self._settings.xiaozhi_udp_enabled or self._settings.rva_udp_enabled) and not udp_ready
        xiaozhi_profiles: tuple[str, ...] = ("wss-opus-v1",)
        if self._settings.xiaozhi_udp_enabled and udp_ready:
            xiaozhi_profiles = ("wss-opus-v1", "udp-opus-gcm-v1")
        bindings = [
            BindingAdvertisement(
                control_protocol="xiaozhi-control-v1",
                public_wss_url=self._settings.worker_public_ws_url,
                profiles=xiaozhi_profiles,
            )
        ]
        profiles = list(xiaozhi_profiles)
        if self._settings.rva_enabled:
            rva_profiles: tuple[str, ...] = ("wss-opus-v2",)
            if self._settings.rva_udp_enabled and udp_ready:
                rva_profiles = ("wss-opus-v2", "udp-opus-gcm-v1")
            bindings.append(
                BindingAdvertisement(
                    control_protocol="rva-control-v1",
                    public_wss_url=self._settings.rva_public_ws_url,
                    profiles=rva_profiles,
                )
            )
            profiles.extend(rva_profiles)
        pending_releases = self._registry.pending_lease_releases() if self._registry is not None else ()
        payload = WorkerHeartbeat(
            worker_id=self._settings.worker_id,
            public_wss_url=self._settings.worker_public_ws_url,
            active_sessions=self._admission.active_count,
            max_sessions=self._settings.max_sessions,
            draining=self._admission.draining,
            healthy=self._provider_readiness.healthy and not udp_unavailable,
            profiles=tuple(dict.fromkeys(profiles)),
            bindings=tuple(bindings),
            active_leases=self._registry.active_lease_renewals() if self._registry is not None else (),
            released_leases=pending_releases,
        )
        response = await self._client.post(
            f"{self._settings.director_url.rstrip('/')}/internal/v1/workers/heartbeat",
            headers={"X-Internal-Token": self._settings.internal_token.get_secret_value()},
            json=payload.model_dump(mode="json"),
        )
        response.raise_for_status()
        body = response.json()
        if self._registry is not None:
            self._registry.acknowledge_lease_releases(pending_releases)
        self._admission.set_draining(bool(body.get("draining", False)))
        if self._registry is not None:
            rejected = {str(value) for value in body.get("rejected_session_epochs", [])}
            lease_expires_at = float(body["lease_expires_at"])
            self._registry.extend_lease_deadlines(lease_expires_at, rejected)
            await self._registry.revoke_session_epochs(rejected)
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
            bind_host=settings.xiaozhi_udp_bind_host,
            bind_port=settings.xiaozhi_udp_bind_port,
            advertised_host=settings.xiaozhi_udp_advertise_host,
            advertised_port=settings.xiaozhi_udp_advertise_port,
            lifetime_seconds=settings.xiaozhi_udp_session_lifetime_seconds,
            probe_timeout_seconds=settings.xiaozhi_udp_probe_timeout_seconds,
            queue_size=settings.xiaozhi_udp_queue_datagrams,
            reorder_wait_seconds=settings.xiaozhi_udp_reorder_wait_ms / 1000,
        )
        if settings.xiaozhi_udp_enabled or settings.rva_udp_enabled
        else None
    )
    xiaozhi_registry = XiaozhiSessionRegistry(settings, admission, udp_gateway=udp_gateway)
    rva_registry = RvaSessionRegistry(settings, admission, udp_gateway=udp_gateway)
    lease_registry = CombinedLeaseRegistry(xiaozhi_registry, rva_registry)
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
        try:
            return await active_grant_consumer.consume(
                verified.director_grant,
                device_id=verified.context.device_id,
            )
        except Exception:
            return False

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.admission = admission
        app.state.xiaozhi_session_registry = xiaozhi_registry
        app.state.rva_session_registry = rva_registry
        app.state.xiaozhi_udp_gateway = udp_gateway
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
            await heartbeat.close()
            await provider_readiness.close()
            await active_grant_consumer.close()
            await asyncio.gather(xiaozhi_registry.close(), rva_registry.close())
            if udp_gateway is not None:
                await udp_gateway.close()

    app = FastAPI(title="Realtime Voice Worker", version="0.1.0", lifespan=lifespan)

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        udp_ready = udp_gateway is None or udp_gateway.is_ready
        required_udp_ready = (
            settings.xiaozhi_transport_policy != "force_udp_for_test" or udp_ready
        ) and (not settings.rva_udp_enabled or udp_ready)
        coordination_ready = not settings.director_url or heartbeat.last_success
        ready_value = (
            not admission.draining and required_udp_ready and provider_readiness.healthy and coordination_ready
        )
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
                "coordination_ready": coordination_ready,
                "rva_enabled": settings.rva_enabled,
                "rva_udp_enabled": settings.rva_udp_enabled,
                "rva_udp_ready": udp_ready if settings.rva_udp_enabled else False,
                "xiaozhi_udp_enabled": settings.xiaozhi_udp_enabled,
                "xiaozhi_udp_ready": udp_ready,
                "xiaozhi_transport_policy": settings.xiaozhi_transport_policy,
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

    @app.websocket("/v1/xiaozhi")
    async def xiaozhi(websocket: WebSocket) -> None:
        device_id = resolve_xiaozhi_device_id(
            websocket.headers.get("device-id"),
            websocket.headers.get("client-id"),
        )
        verified = authenticator.verify(
            websocket.headers.get("authorization"),
            device_id,
            control_protocol="xiaozhi-control-v1",
        )
        if verified is None or websocket.headers.get("protocol-version") != "1":
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="invalid_credentials")
            return
        if not await consume_director_grant(verified):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="grant_rejected")
            return
        await websocket.accept()
        await xiaozhi_registry.run(websocket, verified.context)

    if settings.rva_enabled:

        @app.websocket("/v1/voice")
        async def rva_voice(websocket: WebSocket) -> None:
            device_id = resolve_device_id(websocket.headers.get("device-id"), websocket.headers.get("client-id"))
            verified = authenticator.verify(
                websocket.headers.get("authorization"),
                device_id,
                control_protocol="rva-control-v1",
            )
            enabled_profiles = {"wss-opus-v2"}
            if settings.rva_udp_enabled:
                enabled_profiles.add("udp-opus-gcm-v1")
            if verified is None or not enabled_profiles.intersection(verified.context.allowed_profiles):
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="invalid_credentials")
                return
            reservation = await rva_registry.reserve(verified.context)
            if reservation is None:
                await websocket.close(code=1_013, reason="session_overloaded")
                return
            if not await consume_director_grant(verified):
                await rva_registry.release_reservation(reservation)
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="grant_rejected")
                return
            try:
                await websocket.accept()
            except BaseException:
                await rva_registry.release_reservation(reservation)
                raise
            await rva_registry.run(websocket, verified.context, reservation)

    return app
