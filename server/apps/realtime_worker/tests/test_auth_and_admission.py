from __future__ import annotations

import time

import pytest
from realtime_worker.auth import WorkerAuthenticator, device_ref, resolve_device_id
from realtime_worker.config import Settings
from voice_contracts import ConnectGrantClaims, GrantCodec


def _settings(**changes: object) -> Settings:
    values: dict[str, object] = {
        "worker_id": "worker-a",
        "lab_token": "lab-test-token",
        "grant_signing_key": "validator-grant-signing-key-with-32-bytes",
        "internal_token": "validator-internal-token",
    }
    values.update(changes)
    return Settings(_env_file=None, **values)


def _grant(*, worker_id: str = "worker-a", device_id: str = "device-1") -> str:
    now = time.time()
    claims = ConnectGrantClaims(
        tenant_id="tenant-1",
        device_id=device_id,
        worker_id=worker_id,
        session_epoch="epoch-1",
        fencing_token=3,
        profiles=("wss-opus-v3",),
        control_protocol="rva-control-v2",
        iat=now,
        exp=now + 30,
        jti="jti-1",
    )
    return GrantCodec("validator-grant-signing-key-with-32-bytes").issue(claims)


def test_director_grant_is_worker_device_protocol_and_profile_bound() -> None:
    token = _grant()
    accepted = WorkerAuthenticator(_settings()).verify(f"Bearer {token}", "device-1")

    assert accepted is not None
    assert accepted.context.control_protocol == "rva-control-v2"
    assert accepted.context.allowed_profiles == ("wss-opus-v3",)
    assert accepted.director_grant == token
    assert WorkerAuthenticator(_settings(worker_id="worker-b")).verify(f"Bearer {token}", "device-1") is None
    assert WorkerAuthenticator(_settings()).verify(f"Bearer {token}", "device-2") is None


def test_lab_auth_exposes_only_current_profiles() -> None:
    wss = WorkerAuthenticator(_settings()).verify("Bearer lab-test-token", "device-1")
    udp = WorkerAuthenticator(_settings(rva_udp_enabled=True)).verify("Bearer lab-test-token", "device-1")

    assert wss is not None and wss.context.allowed_profiles == ("wss-opus-v3",)
    assert udp is not None and udp.context.allowed_profiles == ("wss-opus-v3", "udp-opus-gcm-v2")


def test_auth_rejects_missing_invalid_and_disabled_lab_credentials() -> None:
    auth = WorkerAuthenticator(_settings())
    assert auth.verify(None, "device-1") is None
    assert auth.verify("Basic value", "device-1") is None
    assert auth.verify("Bearer lab-test-token", None) is None
    assert WorkerAuthenticator(_settings(allow_lab_auth=False)).verify("Bearer lab-test-token", "device-1") is None


def test_device_identity_is_stable_ascii_and_logs_use_keyed_reference() -> None:
    assert resolve_device_id("8c:bf:ea:04:9e:88", None) == "8c:bf:ea:04:9e:88"
    assert resolve_device_id("device-一", None) is None
    first = device_ref("tenant-a", "device-1", "key-a")
    assert first == device_ref("tenant-a", "device-1", "key-a")
    assert first != device_ref("tenant-b", "device-1", "key-a")
    assert "device-1" not in first


@pytest.mark.parametrize("public_url", ["wss:not-an-authority", "https://worker.test/v2/voice", "wss://worker.test/wrong"])
def test_public_url_requires_canonical_secure_voice_endpoint(public_url: str) -> None:
    with pytest.raises(ValueError, match="VOICE_RVA_PUBLIC_WS_URL"):
        _settings(rva_public_ws_url=public_url).validate_runtime()
