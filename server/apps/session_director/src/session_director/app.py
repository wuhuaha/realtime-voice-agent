from __future__ import annotations

import hashlib
import hmac
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from redis.exceptions import TimeoutError as RedisTimeoutError
from voice_contracts import (
    BootstrapRequest,
    BootstrapResponse,
    DrainRequest,
    GrantCodec,
    GrantConsumeRequest,
    GrantConsumeResponse,
    RouteReleaseRequest,
    RouteReleaseResponse,
    WorkerHeartbeat,
)

from .config import DirectorSettings
from .service import DirectorService, GrantConsumeError, NoCapacityError
from .store import (
    CoordinationStorePort,
    InMemoryCoordinationStore,
    LeaseConflictError,
    RedisCoordinationStore,
    WorkerNotFoundError,
)

logger = logging.getLogger(__name__)


def _bearer_matches(authorization: str | None, expected: str) -> bool:
    if authorization is None:
        return False
    scheme, _, supplied = authorization.partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(supplied, expected)


def create_app(
    settings: DirectorSettings | None = None,
    *,
    store: CoordinationStorePort | None = None,
) -> FastAPI:
    settings = settings or DirectorSettings()
    settings.validate_runtime()
    configured_store = store

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        active_store = configured_store or _create_store(settings)
        app.state.coordination_store = active_store
        app.state.director_service = DirectorService(
            active_store,
            GrantCodec(settings.grant_signing_key.get_secret_value()),
            heartbeat_ttl_seconds=settings.worker_heartbeat_ttl_seconds,
            lease_ttl_seconds=settings.route_lease_ttl_seconds,
        )
        try:
            yield
        finally:
            await active_store.close()

    app = FastAPI(title="Voice Session Director", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(RedisTimeoutError)
    async def redis_timeout_handler(_: Request, __: RedisTimeoutError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "coordination_unavailable"},
        )

    def service() -> DirectorService:
        return app.state.director_service

    def require_internal(x_internal_token: str | None = Header(default=None)) -> None:
        _require_internal(x_internal_token, settings)

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        ready_value = await app.state.coordination_store.ping()
        return JSONResponse(
            status_code=status.HTTP_200_OK if ready_value else status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "ready" if ready_value else "not_ready", "coordination": settings.coordination_backend},
        )

    @app.post("/v1/session/bootstrap", response_model=BootstrapResponse)
    async def bootstrap(
        request: BootstrapRequest,
        authorization: str | None = Header(default=None),
    ) -> BootstrapResponse:
        if not _device_matches(authorization, request, settings):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_credentials")
        try:
            return await service().bootstrap(request)
        except NoCapacityError as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="no_capacity") from exc
        except LeaseConflictError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="route_already_leased") from exc

    @app.post("/v1/session/release", response_model=RouteReleaseResponse)
    async def release(
        request: RouteReleaseRequest,
        authorization: str | None = Header(default=None),
    ) -> RouteReleaseResponse:
        if not _device_matches(authorization, request, settings):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_credentials")
        await service().release(request)
        # A stale or repeated release is intentionally indistinguishable from a successful release.
        return RouteReleaseResponse()

    @app.post("/internal/v1/workers/heartbeat")
    async def heartbeat(
        request: WorkerHeartbeat,
        _: None = Depends(require_internal),
    ):  # type: ignore[no-untyped-def]
        return await service().heartbeat(request)

    @app.post("/internal/v1/workers/{worker_id}/drain")
    async def drain(
        worker_id: str,
        request: DrainRequest,
        _: None = Depends(require_internal),
    ):  # type: ignore[no-untyped-def]
        try:
            return await service().set_draining(worker_id, request.draining)
        except WorkerNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="worker_not_found") from exc

    @app.post("/internal/v1/grants/consume", response_model=GrantConsumeResponse)
    async def consume_grant(
        request: GrantConsumeRequest,
        _: None = Depends(require_internal),
    ) -> GrantConsumeResponse | JSONResponse:
        try:
            claims = await service().consume_grant(
                request.token,
                worker_id=request.worker_id,
                device_id=request.device_id,
            )
        except GrantConsumeError as exc:
            logger.warning(
                "director_grant_consume_rejected reason=%s worker_id=%s device_ref=%s token_length=%d",
                exc.reason,
                request.worker_id,
                _device_ref(f"grant-consume:{request.worker_id}", request.device_id, settings),
                len(request.token),
            )
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={"detail": "grant_rejected", "reason": exc.reason},
            )
        logger.info(
            "director_grant_consumed worker_id=%s device_ref=%s session_epoch=%s fencing_token=%d",
            request.worker_id,
            _device_ref(claims.tenant_id, claims.device_id, settings),
            claims.session_epoch,
            claims.fencing_token,
        )
        return GrantConsumeResponse(
            session_epoch=claims.session_epoch,
            fencing_token=claims.fencing_token,
            expires_at=claims.exp,
        )

    @app.get("/internal/v1/workers")
    async def workers(_: None = Depends(require_internal)):  # type: ignore[no-untyped-def]
        return await app.state.coordination_store.list_workers(now=time.time())

    return app


def _device_matches(
    authorization: str | None,
    request: BootstrapRequest | RouteReleaseRequest,
    settings: DirectorSettings,
) -> bool:
    tenant_credentials = settings.device_credentials.get(request.tenant_id, {})
    credential = tenant_credentials.get(request.device_id)
    if credential is not None and _bearer_matches(authorization, credential.get_secret_value()):
        return True
    return settings.allow_shared_bootstrap_auth and _bearer_matches(
        authorization,
        settings.device_bootstrap_token.get_secret_value(),
    )


def _require_internal(value: str | None, settings: DirectorSettings) -> None:
    if value is None or not hmac.compare_digest(value, settings.internal_token.get_secret_value()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_internal_credentials")


def _device_ref(namespace: str, device_id: str, settings: DirectorSettings) -> str:
    message = f"{namespace}\0{device_id}".encode()
    key = settings.internal_token.get_secret_value().encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()[:12]


def _create_store(settings: DirectorSettings) -> CoordinationStorePort:
    if settings.coordination_backend == "redis":
        return RedisCoordinationStore(
            Redis.from_url(
                settings.redis_url,
                decode_responses=False,
                socket_connect_timeout=settings.redis_connect_timeout_seconds,
                socket_timeout=settings.redis_command_timeout_seconds,
            ),
            prefix=settings.coordination_prefix,
        )
    return InMemoryCoordinationStore()
