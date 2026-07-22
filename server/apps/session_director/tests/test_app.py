from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
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
                "public_wss_url": "ws://worker-a.test/v1/voice",
                "active_sessions": 0,
                "max_sessions": 5,
                "draining": False,
                "healthy": True,
                "profiles": ["wss-opus-v2", "udp-opus-gcm-v1"],
            },
        )
        assert heartbeat.status_code == 200

        bootstrap = client.post(
            "/v1/session/bootstrap",
            headers={"Authorization": "Bearer validator-device-token"},
            json={"tenant_id": "tenant-1", "device_id": "device-1", "supported_profiles": ["wss-opus-v2"]},
        )
        assert bootstrap.status_code == 200
        body = bootstrap.json()
        assert body["worker_id"] == "worker-a"
        assert body["allowed_profiles"] == ["wss-opus-v2"]
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


def test_internal_and_bootstrap_credentials_are_separate() -> None:
    app = create_app(settings(), store=InMemoryCoordinationStore())
    with TestClient(app) as client:
        assert client.post("/internal/v1/workers/heartbeat", json={}).status_code == 401
        assert client.post("/v1/session/bootstrap", json={"device_id": "device-1"}).status_code == 401


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
