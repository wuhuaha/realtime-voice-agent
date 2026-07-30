from __future__ import annotations

import asyncio
import math

import pytest

from rva_desktop.config import ClientConfig, MediaProfile
from rva_desktop.errors import ProtocolError, TransportError
from rva_desktop.protocol import UdpGrant
from rva_desktop.transport.director import BootstrapGrant, DirectorClient


class _Response:
    def __init__(self, status_code: int, body: object) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> object:
        return self._body


class _HttpClient:
    def __init__(self, response: _Response) -> None:
        self.response = response

    async def post(self, _url: str, **_kwargs: object) -> _Response:
        return self.response

    async def aclose(self) -> None:
        return None


class _FailingHttpClient:
    def __init__(self, failure: Exception) -> None:
        self.failure = failure

    async def post(self, _url: str, **_kwargs: object) -> _Response:
        raise self.failure

    async def aclose(self) -> None:
        return None


class _InvalidJsonResponse:
    status_code = 200

    def json(self) -> object:
        raise ValueError("malformed response body")


def _config(**updates: object) -> ClientConfig:
    values = {
        "director_url": "https://director.test",
        "bootstrap_token": "bootstrap-secret",
        "device_id": "desktop-1",
        "supported_profiles": (MediaProfile.WSS_OPUS_V3,),
        "preferred_profile": MediaProfile.WSS_OPUS_V3,
    }
    values.update(updates)
    return ClientConfig(**values)  # type: ignore[arg-type]


def _bootstrap(worker_url: str) -> dict[str, object]:
    return {
        "worker_id": "worker-1",
        "worker_wss_url": worker_url,
        "connect_grant": "connect-secret-value",
        "session_epoch": "epoch-1",
        "fencing_token": 1,
        "allowed_profiles": ["wss-opus-v3"],
        "control_protocol": "rva-control-v2",
        "expires_at": 100.0,
    }


def test_plain_http_requires_explicit_loopback_policy() -> None:
    with pytest.raises(ValueError, match="loopback"):
        _config(director_url="http://127.0.0.1:8080")
    with pytest.raises(ValueError, match="loopback"):
        _config(director_url="http://voice.example.test")
    assert _config(
        director_url="http://127.0.0.1:8080",
        allow_insecure_loopback=True,
    ).allow_insecure_loopback


def test_secure_director_rejects_plain_worker_downgrade() -> None:
    async def scenario() -> None:
        client = DirectorClient(
            _config(),
            client=_HttpClient(_Response(200, _bootstrap("ws://worker.test/v2/voice"))),
            wall_clock=lambda: 0.0,
        )
        with pytest.raises(ProtocolError, match="insecure_worker_wss_url"):
            await client.bootstrap()

    asyncio.run(scenario())


def test_explicit_loopback_policy_accepts_only_loopback_worker() -> None:
    async def scenario() -> None:
        config = _config(
            director_url="http://127.0.0.1:8080",
            allow_insecure_loopback=True,
        )
        accepted = DirectorClient(
            config,
            client=_HttpClient(_Response(200, _bootstrap("ws://127.0.0.1:8081/v2/voice"))),
            wall_clock=lambda: 0.0,
        )
        grant = await accepted.bootstrap()
        assert grant.worker_wss_url.startswith("ws://127.0.0.1")

        rejected = DirectorClient(
            config,
            client=_HttpClient(_Response(200, _bootstrap("ws://voice.example.test/v2/voice"))),
            wall_clock=lambda: 0.0,
        )
        with pytest.raises(ProtocolError, match="insecure_worker_wss_url"):
            await rejected.bootstrap()

    asyncio.run(scenario())


def test_bootstrap_4xx_is_not_retryable() -> None:
    async def scenario() -> None:
        client = DirectorClient(
            _config(),
            client=_HttpClient(_Response(422, {})),
            wall_clock=lambda: 0.0,
        )
        with pytest.raises(ProtocolError) as captured:
            await client.bootstrap()
        assert captured.value.retryable is False

    asyncio.run(scenario())


def test_secret_material_is_absent_from_dataclass_repr() -> None:
    async def scenario() -> None:
        client = DirectorClient(
            _config(),
            client=_HttpClient(_Response(200, _bootstrap("wss://worker.test/v2/voice"))),
            wall_clock=lambda: 0.0,
        )
        grant = await client.bootstrap()
        assert "connect-secret-value" not in repr(grant)

    asyncio.run(scenario())
    udp = UdpGrant("voice.test", 8443, 100, 1000, b"a" * 16, b"b" * 8, b"c" * 16, b"d" * 8, 500)
    assert "aaaaaaaa" not in repr(udp)
    assert "bbbbbbbb" not in repr(udp)


@pytest.mark.parametrize("failure", [TimeoutError("timed out"), OSError("connection lost")])
def test_bootstrap_network_failures_are_stable_retryable_transport_errors(failure: Exception) -> None:
    async def scenario() -> None:
        client = DirectorClient(_config(), client=_FailingHttpClient(failure))

        with pytest.raises(TransportError) as captured:
            await client.bootstrap()

        assert captured.value.code == "bootstrap_transport_failed"
        assert captured.value.retryable is True
        assert captured.value.__cause__ is failure

    asyncio.run(scenario())


def test_release_network_failure_is_a_stable_retryable_transport_error() -> None:
    async def scenario() -> None:
        failure = TimeoutError("timed out")
        client = DirectorClient(_config(), client=_FailingHttpClient(failure))
        grant = BootstrapGrant(
            worker_id="worker-1",
            worker_wss_url="wss://worker.test/v2/voice",
            connect_grant="connect-secret-value",
            session_epoch="epoch-1",
            fencing_token=1,
            allowed_profiles=(MediaProfile.WSS_OPUS_V3,),
            expires_at=100.0,
        )

        with pytest.raises(TransportError) as captured:
            await client.release(grant)

        assert captured.value.code == "release_transport_failed"
        assert captured.value.retryable is True
        assert captured.value.__cause__ is failure

    asyncio.run(scenario())


def test_bootstrap_json_failure_is_a_non_retryable_protocol_error() -> None:
    async def scenario() -> None:
        client = DirectorClient(_config(), client=_HttpClient(_InvalidJsonResponse()))  # type: ignore[arg-type]

        with pytest.raises(ProtocolError) as captured:
            await client.bootstrap()

        assert captured.value.code == "invalid_bootstrap_response"
        assert captured.value.retryable is False
        assert isinstance(captured.value.__cause__, ValueError)

    asyncio.run(scenario())


@pytest.mark.parametrize("expires_at", [math.nan, math.inf, -math.inf])
def test_bootstrap_rejects_non_finite_expiry(expires_at: float) -> None:
    async def scenario() -> None:
        body = _bootstrap("wss://worker.test/v2/voice")
        body["expires_at"] = expires_at
        client = DirectorClient(
            _config(),
            client=_HttpClient(_Response(200, body)),
            wall_clock=lambda: 0.0,
        )

        with pytest.raises(ProtocolError) as captured:
            await client.bootstrap()

        assert captured.value.code == "invalid_bootstrap_response"
        assert captured.value.retryable is False

    asyncio.run(scenario())


def test_bootstrap_rejects_non_string_profile_values() -> None:
    async def scenario() -> None:
        body = _bootstrap("wss://worker.test/v2/voice")
        body["allowed_profiles"] = [MediaProfile.WSS_OPUS_V3]
        client = DirectorClient(
            _config(),
            client=_HttpClient(_Response(200, body)),
            wall_clock=lambda: 0.0,
        )

        with pytest.raises(ProtocolError, match="invalid_bootstrap_response"):
            await client.bootstrap()

    asyncio.run(scenario())
