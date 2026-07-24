from __future__ import annotations

import logging

import pytest
import session_director.app as director_app
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from redis.exceptions import TimeoutError as RedisTimeoutError
from session_director.app import create_app
from session_director.config import DirectorSettings
from session_director.store import InMemoryCoordinationStore


def settings() -> DirectorSettings:
    return DirectorSettings(
        _env_file=None,
        coordination_backend="memory",
        internal_token="validator-internal-token",
        grant_signing_key="validator-grant-signing-key-with-32-bytes",
        device_bootstrap_token="validator-device-token",
    )


def test_heartbeat_bootstrap_and_drain_contract() -> None:
    app = create_app(settings(), store=InMemoryCoordinationStore())
    internal = {"X-Internal-Token": "validator-internal-token"}
    with TestClient(app) as client:
        heartbeat = client.post(
            "/internal/v1/workers/heartbeat",
            headers=internal,
            json={
                "worker_id": "worker-a",
                "public_wss_url": "ws://worker-a.test/v2/voice",
                "active_sessions": 0,
                "max_sessions": 5,
                "draining": False,
                "healthy": True,
                "profiles": ["wss-opus-v3", "udp-opus-gcm-v2"],
            },
        )
        assert heartbeat.status_code == 200

        bootstrap = client.post(
            "/v1/session/bootstrap",
            headers={"Authorization": "Bearer validator-device-token"},
            json={"tenant_id": "tenant-1", "device_id": "device-1", "supported_profiles": ["wss-opus-v3"]},
        )
        assert bootstrap.status_code == 200
        body = bootstrap.json()
        assert body["worker_id"] == "worker-a"
        assert body["allowed_profiles"] == ["wss-opus-v3"]
        assert body["control_protocol"] == "rva-control-v2"
        assert body["worker_wss_url"] == "ws://worker-a.test/v2/voice"
        assert body["connect_grant"].count(".") == 2

        consumed = client.post(
            "/internal/v1/grants/consume",
            headers=internal,
            json={
                "token": body["connect_grant"],
                "worker_id": "worker-a",
                "device_id": "device-1",
            },
        )
        assert consumed.status_code == 200
        assert consumed.json()["session_epoch"] == body["session_epoch"]
        replay = client.post(
            "/internal/v1/grants/consume",
            headers=internal,
            json={
                "token": body["connect_grant"],
                "worker_id": "worker-a",
                "device_id": "device-1",
            },
        )
        assert replay.status_code == 409

        drained = client.post(
            "/internal/v1/workers/worker-a/drain",
            headers=internal,
            json={"draining": True},
        )
        assert drained.status_code == 200
        rejected = client.post(
            "/v1/session/bootstrap",
            headers={"Authorization": "Bearer validator-device-token"},
            json={"device_id": "device-2"},
        )
        assert rejected.status_code == 503


def test_grant_consume_reject_reason_is_specific_and_redacted(caplog: pytest.LogCaptureFixture) -> None:
    app = create_app(settings(), store=InMemoryCoordinationStore())
    internal = {"X-Internal-Token": "validator-internal-token"}
    caplog.set_level(logging.WARNING, logger="session_director.app")
    with TestClient(app) as client:
        assert client.post(
            "/internal/v1/workers/heartbeat",
            headers=internal,
            json={
                "worker_id": "worker-a",
                "public_wss_url": "ws://worker-a.test/v2/voice",
                "active_sessions": 0,
                "profiles": ["wss-opus-v3"],
            },
        ).status_code == 200
        opened = client.post(
            "/v1/session/bootstrap",
            headers={"Authorization": "Bearer validator-device-token"},
            json={"tenant_id": "tenant-1", "device_id": "device-1", "supported_profiles": ["wss-opus-v3"]},
        ).json()

        consumed = client.post(
            "/internal/v1/grants/consume",
            headers=internal,
            json={"token": opened["connect_grant"], "worker_id": "worker-a", "device_id": "device-1"},
        )
        replay = client.post(
            "/internal/v1/grants/consume",
            headers=internal,
            json={"token": opened["connect_grant"], "worker_id": "worker-a", "device_id": "device-1"},
        )
        corrupted_token = opened["connect_grant"][:-1] + ("A" if opened["connect_grant"][-1] != "A" else "B")
        invalid_signature = client.post(
            "/internal/v1/grants/consume",
            headers=internal,
            json={"token": corrupted_token, "worker_id": "worker-a", "device_id": "device-1"},
        )

    assert consumed.status_code == 200
    assert replay.status_code == 409
    assert replay.json() == {"detail": "grant_rejected", "reason": "grant_replay"}
    assert invalid_signature.status_code == 409
    assert invalid_signature.json() == {"detail": "grant_rejected", "reason": "invalid_grant_signature"}
    assert "reason=grant_replay" in caplog.text
    assert "reason=invalid_grant_signature" in caplog.text
    assert opened["connect_grant"] not in caplog.text
    assert corrupted_token not in caplog.text
    assert "validator-grant-signing-key-with-32-bytes" not in caplog.text
    assert "validator-internal-token" not in caplog.text
    assert "device-1" not in caplog.text
    assert "device_ref=" in caplog.text


def test_internal_and_bootstrap_credentials_are_separate() -> None:
    app = create_app(settings(), store=InMemoryCoordinationStore())
    with TestClient(app) as client:
        assert client.post("/internal/v1/workers/heartbeat", json={}).status_code == 401
        assert client.post("/v1/session/bootstrap", json={"device_id": "device-1"}).status_code == 401


@pytest.mark.parametrize("public_wss_url", ["ws://worker-a.test/v1/voice", "ws://worker-a.test/v1/xiaozhi"])
def test_heartbeat_rejects_legacy_worker_routes(public_wss_url: str) -> None:
    app = create_app(settings(), store=InMemoryCoordinationStore())
    with TestClient(app) as client:
        response = client.post(
            "/internal/v1/workers/heartbeat",
            headers={"X-Internal-Token": "validator-internal-token"},
            json={
                "worker_id": "worker-a",
                "public_wss_url": public_wss_url,
                "active_sessions": 0,
                "profiles": ["wss-opus-v3"],
            },
        )

    assert response.status_code == 422


def test_authenticated_release_is_idempotent_and_allows_immediate_bootstrap() -> None:
    app = create_app(settings(), store=InMemoryCoordinationStore())
    device = {"Authorization": "Bearer validator-device-token"}
    internal = {"X-Internal-Token": "validator-internal-token"}
    with TestClient(app) as client:
        assert client.post(
            "/internal/v1/workers/heartbeat",
            headers=internal,
            json={
                "worker_id": "worker-a",
                "public_wss_url": "ws://worker-a.test/v2/voice",
                "active_sessions": 0,
            },
        ).status_code == 200
        opened = client.post(
            "/v1/session/bootstrap",
            headers=device,
            json={"tenant_id": "tenant-1", "device_id": "device-1"},
        ).json()
        release = {
            "tenant_id": "tenant-1",
            "device_id": "device-1",
            "worker_id": opened["worker_id"],
            "session_epoch": opened["session_epoch"],
            "fencing_token": opened["fencing_token"],
        }

        assert client.post("/v1/session/release", headers=device, json=release).json() == {"released": True}
        reopened = client.post(
            "/v1/session/bootstrap",
            headers=device,
            json={"tenant_id": "tenant-1", "device_id": "device-1"},
        )
        assert reopened.status_code == 200
        assert reopened.json()["fencing_token"] == opened["fencing_token"] + 1
        assert client.post("/v1/session/release", headers=device, json=release).json() == {"released": True}
        assert client.post(
            "/v1/session/bootstrap",
            headers=device,
            json={"tenant_id": "tenant-1", "device_id": "device-1"},
        ).status_code == 409


def test_release_requires_credentials_bound_to_request_principal() -> None:
    configured = settings().model_copy(
        update={
            "allow_shared_bootstrap_auth": False,
            "device_credentials": {"tenant-1": {"device-1": SecretStr("validator-device-1-token")}},
        }
    )
    app = create_app(configured, store=InMemoryCoordinationStore())
    release = {
        "tenant_id": "tenant-1",
        "device_id": "device-2",
        "worker_id": "worker-a",
        "session_epoch": "epoch-1",
        "fencing_token": 1,
    }
    with TestClient(app) as client:
        missing = client.post("/v1/session/release", json=release)
        wrong_principal = client.post(
            "/v1/session/release",
            headers={"Authorization": "Bearer validator-device-1-token"},
            json=release,
        )

    assert missing.status_code == 401
    assert wrong_principal.status_code == 401


def test_device_specific_bootstrap_credential_is_bound_to_tenant_and_device() -> None:
    configured = settings().model_copy(
        update={
            "allow_shared_bootstrap_auth": False,
            "device_credentials": {"tenant-1": {"device-1": SecretStr("validator-device-1-token")}},
        }
    )
    app = create_app(configured, store=InMemoryCoordinationStore())
    with TestClient(app) as client:
        wrong = client.post(
            "/v1/session/bootstrap",
            headers={"Authorization": "Bearer validator-device-1-token"},
            json={"tenant_id": "tenant-1", "device_id": "device-2"},
        )
        assert wrong.status_code == 401


def test_production_director_rejects_shared_or_missing_device_credentials() -> None:
    with pytest.raises(ValueError, match="COORDINATION_BACKEND"):
        settings().model_copy(update={"environment": "production"}).validate_runtime()


def test_drain_contract_is_one_way() -> None:
    app = create_app(settings(), store=InMemoryCoordinationStore())
    with TestClient(app) as client:
        response = client.post(
            "/internal/v1/workers/worker-a/drain",
            headers={"X-Internal-Token": "validator-internal-token"},
            json={"draining": False},
        )
        assert response.status_code == 422


def test_redis_client_uses_configured_connection_and_command_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    configured = settings().model_copy(
        update={
            "coordination_backend": "redis",
            "redis_connect_timeout_seconds": 0.4,
            "redis_command_timeout_seconds": 0.7,
        }
    )
    captured: dict[str, object] = {}

    def fake_from_url(url: str, **kwargs: object) -> object:
        captured["url"] = url
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(director_app.Redis, "from_url", fake_from_url)

    director_app._create_store(configured)

    assert captured == {
        "url": configured.redis_url,
        "decode_responses": False,
        "socket_connect_timeout": 0.4,
        "socket_timeout": 0.7,
    }


class _TimeoutCoordinationStore(InMemoryCoordinationStore):
    async def ping(self) -> bool:
        raise RedisTimeoutError("redis://user:secret@internal.example")

    async def list_workers(self, *, now: float):  # type: ignore[no-untyped-def]
        raise RedisTimeoutError("redis://user:secret@internal.example")


def test_bootstrap_redis_timeout_returns_generic_service_unavailable() -> None:
    app = create_app(settings(), store=_TimeoutCoordinationStore())

    with TestClient(app, raise_server_exceptions=False) as client:
        ready = client.get("/health/ready")
        response = client.post(
            "/v1/session/bootstrap",
            headers={"Authorization": "Bearer validator-device-token"},
            json={"tenant_id": "tenant-1", "device_id": "device-1", "supported_profiles": ["wss-opus-v3"]},
        )

    assert ready.status_code == 503
    assert ready.json() == {"detail": "coordination_unavailable"}
    assert "secret" not in ready.text
    assert response.status_code == 503
    assert response.json() == {"detail": "coordination_unavailable"}
    assert "secret" not in response.text


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("redis_connect_timeout_seconds", 0.01),
        ("redis_connect_timeout_seconds", 10.01),
        ("redis_command_timeout_seconds", 0.01),
        ("redis_command_timeout_seconds", 10.01),
    ],
)
def test_redis_timeouts_are_bounded(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        DirectorSettings(_env_file=None, **{field: value})
