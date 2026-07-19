from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import ssl
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Header, HTTPException, WebSocket, status
from fastapi.responses import JSONResponse
from voice_contracts import WorkerHeartbeat

from .auth import WorkerAuthenticator
from .bindings.xiaozhi import SharedSessionAdmission, XiaozhiSessionRegistry, resolve_xiaozhi_device_id
from .bindings.xiaozhi_udp import UdpMediaGateway
from .config import Settings

logger = logging.getLogger(__name__)


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
        registry: XiaozhiSessionRegistry | None = None,
        provider_readiness: ProviderReadiness | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._admission = admission
        self._registry = registry
        self._provider_readiness = provider_readiness or ProviderReadiness(settings)
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
        profiles: tuple[str, ...] = ("wss-opus-v1",)
        if self._settings.xiaozhi_udp_enabled:
            profiles = ("wss-opus-v1", "udp-opus-gcm-v1")
        pending_releases = self._registry.pending_lease_releases() if self._registry is not None else ()
        payload = WorkerHeartbeat(
            worker_id=self._settings.worker_id,
            public_wss_url=self._settings.worker_public_ws_url,
            active_sessions=self._admission.active_count,
            max_sessions=self._settings.max_sessions,
            draining=self._admission.draining,
            healthy=self._provider_readiness.healthy,
            profiles=profiles,
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
        if settings.xiaozhi_udp_enabled
        else None
    )
    registry = XiaozhiSessionRegistry(settings, admission, udp_gateway=udp_gateway)
    provider_readiness = ProviderReadiness(settings)
    heartbeat = WorkerHeartbeatLoop(settings, admission, registry, provider_readiness)
    active_grant_consumer = grant_consumer or DirectorGrantConsumer(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.admission = admission
        app.state.xiaozhi_session_registry = registry
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
            await registry.close()
            if udp_gateway is not None:
                await udp_gateway.close()

    app = FastAPI(title="Realtime Voice Worker", version="0.1.0", lifespan=lifespan)

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        udp_ready = udp_gateway is None or udp_gateway.is_ready
        required_udp_ready = settings.xiaozhi_transport_policy != "force_udp_for_test" or udp_ready
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
        device_id = resolve_xiaozhi_device_id(websocket.headers.get("device-id"), websocket.headers.get("client-id"))
        verified = authenticator.verify(websocket.headers.get("authorization"), device_id)
        if verified is None or websocket.headers.get("protocol-version") != "1":
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="invalid_credentials")
            return
        if verified.director_grant is not None:
            try:
                consumed = await active_grant_consumer.consume(
                    verified.director_grant,
                    device_id=verified.context.device_id,
                )
            except Exception:
                consumed = False
            if not consumed:
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="grant_rejected")
                return
        await websocket.accept()
        await registry.run(websocket, verified.context)

    return app
