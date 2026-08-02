from __future__ import annotations

import math
import time
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any, Protocol
from urllib.parse import urlsplit

from ..config import ClientConfig, MediaProfile, _loopback_host
from ..errors import AuthenticationError, ProtocolError, TransportError
from ..trace import NullTrace, TraceSink


class HttpResponse(Protocol):
    status_code: int

    def json(self) -> Any: ...


class HttpClient(Protocol):
    async def post(self, url: str, **kwargs: object) -> HttpResponse: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class BootstrapGrant:
    worker_id: str
    worker_wss_url: str
    connect_grant: str = dataclass_field(repr=False)
    session_epoch: str
    fencing_token: int
    allowed_profiles: tuple[MediaProfile, ...]
    expires_at: float


class DirectorClient:
    def __init__(
        self,
        config: ClientConfig,
        *,
        client: HttpClient | None = None,
        trace: TraceSink | None = None,
        wall_clock=time.time,
    ) -> None:
        self._config = config
        self._client = client
        self._owns_client = client is None
        self._trace = trace or NullTrace()
        self._wall_clock = wall_clock

    async def bootstrap(self) -> BootstrapGrant:
        client = self._client or self._default_client()
        self._client = client
        try:
            response = await client.post(
                f"{self._config.director_url.rstrip('/')}/v1/session/bootstrap",
                headers={"Authorization": f"Bearer {self._config.bootstrap_token}"},
                json={
                    "tenant_id": self._config.tenant_id,
                    "device_id": self._config.device_id,
                    "supported_profiles": [item.value for item in self._config.supported_profiles],
                    "control_protocol": "rva/1",
                },
            )
        except Exception as exc:
            raise TransportError("bootstrap_transport_failed", type(exc).__name__, retryable=True) from exc
        if response.status_code in {401, 403}:
            raise AuthenticationError("director_auth_rejected")
        if 400 <= response.status_code < 500 and response.status_code != 429:
            raise ProtocolError("bootstrap_request_rejected", f"Director returned HTTP {response.status_code}")
        if response.status_code != 200:
            raise TransportError("bootstrap_failed", f"Director returned HTTP {response.status_code}", retryable=True)
        try:
            body = response.json()
        except Exception as exc:
            raise ProtocolError("invalid_bootstrap_response") from exc
        grant = self._parse_grant(body)
        self._trace.emit(
            "director.bootstrap.accepted",
            {
                "worker_id": grant.worker_id,
                "session_epoch": grant.session_epoch,
                "allowed_profiles": [item.value for item in grant.allowed_profiles],
            },
        )
        return grant

    async def release(self, grant: BootstrapGrant) -> None:
        if self._client is None:
            return
        try:
            response = await self._client.post(
                f"{self._config.director_url.rstrip('/')}/v1/session/release",
                headers={"Authorization": f"Bearer {self._config.bootstrap_token}"},
                json={
                    "tenant_id": self._config.tenant_id,
                    "device_id": self._config.device_id,
                    "worker_id": grant.worker_id,
                    "session_epoch": grant.session_epoch,
                    "fencing_token": grant.fencing_token,
                },
            )
        except Exception as exc:
            raise TransportError("release_transport_failed", type(exc).__name__, retryable=True) from exc
        if response.status_code not in {200, 404}:
            self._trace.emit("director.release.failed", {"status_code": response.status_code})

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
        self._client = None

    def _parse_grant(self, value: object) -> BootstrapGrant:
        if not isinstance(value, dict):
            raise ProtocolError("invalid_bootstrap_response")
        expected = {
            "worker_id", "worker_wss_url", "connect_grant", "session_epoch", "fencing_token",
            "allowed_profiles", "control_protocol", "expires_at",
        }
        if set(value) != expected or value.get("control_protocol") != "rva/1":
            raise ProtocolError("invalid_bootstrap_response")
        raw_profiles = value["allowed_profiles"]
        if type(raw_profiles) is not list or any(type(item) is not str for item in raw_profiles):
            raise ProtocolError("invalid_bootstrap_response")
        try:
            profiles = tuple(MediaProfile(item) for item in raw_profiles)
        except (ValueError, TypeError) as exc:
            raise ProtocolError("invalid_bootstrap_response") from exc
        if not profiles or not set(profiles).issubset(self._config.supported_profiles):
            raise ProtocolError("invalid_bootstrap_response")
        expires_at = value["expires_at"]
        if type(expires_at) not in {int, float} or not math.isfinite(expires_at):
            raise ProtocolError("invalid_bootstrap_response")
        if expires_at <= self._wall_clock():
            raise ProtocolError("expired_bootstrap_response")
        for field in ("worker_id", "worker_wss_url", "connect_grant", "session_epoch"):
            if not isinstance(value[field], str) or not value[field]:
                raise ProtocolError("invalid_bootstrap_response")
        worker_url = urlsplit(value["worker_wss_url"])
        if (
            worker_url.path != "/rva/v1/voice"
            or worker_url.query
            or worker_url.fragment
            or not worker_url.hostname
            or worker_url.username is not None
            or worker_url.password is not None
            or worker_url.scheme not in {"ws", "wss"}
        ):
            raise ProtocolError("invalid_worker_wss_url")
        director_url = urlsplit(self._config.director_url)
        insecure_loopback = (
            self._config.allow_insecure_loopback
            and director_url.scheme == "http"
            and _loopback_host(director_url.hostname)
            and worker_url.scheme == "ws"
            and _loopback_host(worker_url.hostname)
        )
        if worker_url.scheme != "wss" and not insecure_loopback:
            raise ProtocolError("insecure_worker_wss_url")
        fencing_token = value["fencing_token"]
        if type(fencing_token) is not int or fencing_token < 1:
            raise ProtocolError("invalid_bootstrap_response")
        return BootstrapGrant(
            worker_id=value["worker_id"],
            worker_wss_url=value["worker_wss_url"],
            connect_grant=value["connect_grant"],
            session_epoch=value["session_epoch"],
            fencing_token=fencing_token,
            allowed_profiles=profiles,
            expires_at=float(expires_at),
        )

    def _default_client(self) -> HttpClient:
        import httpx

        return httpx.AsyncClient(timeout=httpx.Timeout(self._config.connect_timeout_seconds))
