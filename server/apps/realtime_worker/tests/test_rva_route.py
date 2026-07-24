from __future__ import annotations

from fastapi.testclient import TestClient
from realtime_worker.app import create_app
from realtime_worker.config import Settings


def _settings(**changes: object) -> Settings:
    values: dict[str, object] = {
        "lab_token": "test-token",
        "grant_signing_key": "validator-grant-signing-key-with-32-bytes",
        "internal_token": "validator-internal-token",
        "runner": "deterministic",
        "heartbeat_enabled": False,
    }
    values.update(changes)
    return Settings(_env_file=None, **values)


def test_worker_registers_only_current_voice_route() -> None:
    app = create_app(_settings())
    paths = {route.path for route in app.routes}

    assert "/v2/voice" in paths
    assert sum(path.endswith("/voice") for path in paths) == 1


def test_current_voice_route_requires_bearer_and_device_identity() -> None:
    app = create_app(_settings())
    with TestClient(app) as client:
        try:
            with client.websocket_connect("/v2/voice"):
                raise AssertionError("unauthenticated websocket unexpectedly opened")
        except Exception as exc:
            assert getattr(exc, "code", None) == 1_008
            assert getattr(exc, "reason", None) == "invalid_credentials"


def test_health_reports_current_rva_runtime_ready() -> None:
    with TestClient(create_app(_settings())) as client:
        response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["rva_enabled"] is True
