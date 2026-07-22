from __future__ import annotations

import logging
import time

import pytest
from realtime_worker.admission import SharedSessionAdmission
from realtime_worker.auth import WorkerAuthenticator, device_ref
from realtime_worker.config import Settings
from voice_contracts import ConnectGrantClaims, GrantCodec


def worker_settings(worker_id: str = "worker-a") -> Settings:
    return Settings(
        _env_file=None,
        worker_id=worker_id,
        lab_token="lab-test-token",
        grant_signing_key="validator-grant-signing-key-with-32-bytes",
        internal_token="validator-internal-token",
    )


def grant(
    worker_id: str = "worker-a",
    *,
    exp: float | None = None,
    jti: str = "jti-1",
    fencing_token: int = 3,
    session_epoch: str = "epoch-1",
    control_protocol: str = "rva-control-v1",
    profiles: tuple[str, ...] = ("wss-opus-v2",),
) -> str:
    now = time.time()
    claims = ConnectGrantClaims(
        tenant_id="tenant-1",
        device_id="device-1",
        worker_id=worker_id,
        session_epoch=session_epoch,
        fencing_token=fencing_token,
        profiles=profiles,
        control_protocol=control_protocol,
        iat=now,
        exp=exp if exp is not None else now + 30,
        jti=jti,
    )
    return GrantCodec("validator-grant-signing-key-with-32-bytes").issue(claims)


def test_director_grant_is_verified_and_worker_bound_before_shared_consumption() -> None:
    auth = WorkerAuthenticator(worker_settings())
    token = grant()

    accepted = auth.verify(f"Bearer {token}", "device-1")
    assert accepted is not None
    assert accepted.context.allowed_profiles == ("wss-opus-v2",)
    assert accepted.context.expires_at is not None
    assert accepted.director_grant == token
    assert WorkerAuthenticator(worker_settings("worker-b")).verify(f"Bearer {grant()}", "device-1") is None


def test_director_grant_preserves_colon_mac_device_principal() -> None:
    now = time.time()
    claims = ConnectGrantClaims(
        tenant_id="default",
        device_id="8c:bf:ea:04:9e:88",
        worker_id="worker-a",
        session_epoch="epoch-mac",
        fencing_token=1,
        profiles=("wss-opus-v2",),
        iat=now,
        exp=now + 30,
        jti="jti-mac",
    )
    token = GrantCodec("validator-grant-signing-key-with-32-bytes").issue(claims)

    accepted = WorkerAuthenticator(worker_settings()).verify(
        f"Bearer {token}",
        "8c:bf:ea:04:9e:88",
    )

    assert accepted is not None
    assert accepted.context.device_id == "8c:bf:ea:04:9e:88"


def test_expired_and_wrong_device_grants_fail_closed() -> None:
    auth = WorkerAuthenticator(worker_settings())
    assert auth.verify(f"Bearer {grant(exp=time.time() - 1, jti='expired')}", "device-1") is None
    assert auth.verify(f"Bearer {grant(jti='wrong-device')}", "device-2") is None


def test_rejected_grant_logs_only_safe_classification(caplog: pytest.LogCaptureFixture) -> None:
    token = grant()
    caplog.set_level(logging.WARNING, logger="realtime_worker.auth")

    assert WorkerAuthenticator(worker_settings("worker-b")).verify(f"Bearer {token}", "device-1") is None

    text = caplog.text
    assert "reason=grant_grant_belongs_to_another_worker" in text
    assert f"token_length={len(token)}" in text
    assert "device_ref=" in text
    assert "device-1" not in text
    assert token not in text


def test_invalid_scheme_log_redacts_device_principal(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="realtime_worker.auth")

    assert WorkerAuthenticator(worker_settings()).verify("Basic credential", "8c:bf:ea:04:9e:88") is None

    assert "reason=invalid_scheme" in caplog.text
    assert "device_ref=" in caplog.text
    assert "8c:bf:ea:04:9e:88" not in caplog.text


def test_device_ref_is_keyed_and_tenant_scoped() -> None:
    first = device_ref("tenant-a", "device-1", "deployment-key-a")

    assert first == device_ref("tenant-a", "device-1", "deployment-key-a")
    assert first != device_ref("tenant-b", "device-1", "deployment-key-a")
    assert first != device_ref("tenant-a", "device-1", "deployment-key-b")
    assert "device-1" not in first


def test_lab_compatibility_does_not_require_director_consumption() -> None:
    auth = WorkerAuthenticator(worker_settings())
    accepted = auth.verify("Bearer lab-test-token", "device-1")
    assert accepted is not None
    assert accepted.director_grant is None
    assert accepted.context.expires_at is None


def test_control_protocol_and_profile_are_bound_before_grant_consumption() -> None:
    auth = WorkerAuthenticator(worker_settings())
    token = grant(control_protocol="rva-control-v1", profiles=("wss-opus-v2",))

    accepted = auth.verify(f"Bearer {token}", "device-1", control_protocol="rva-control-v1")

    assert accepted is not None
    assert accepted.context.control_protocol == "rva-control-v1"
    assert accepted.context.allowed_profiles == ("wss-opus-v2",)
    assert auth.verify(f"Bearer {token}", "device-1", control_protocol="xiaozhi-control-v1") is None


def test_legacy_grant_requires_explicit_xiaozhi_control_selection() -> None:
    auth = WorkerAuthenticator(worker_settings())
    token = grant(control_protocol="xiaozhi-control-v1", profiles=("wss-opus-v1",))

    assert auth.verify(f"Bearer {token}", "device-1") is None
    accepted = auth.verify(
        f"Bearer {token}",
        "device-1",
        control_protocol="xiaozhi-control-v1",
    )
    assert accepted is not None
    assert accepted.context.allowed_profiles == ("wss-opus-v1",)


def test_rva_lab_auth_only_grants_rva_wss_profile() -> None:
    accepted = WorkerAuthenticator(worker_settings()).verify(
        "Bearer lab-test-token",
        "device-1",
        control_protocol="rva-control-v1",
    )

    assert accepted is not None
    assert accepted.context.allowed_profiles == ("wss-opus-v2",)


def test_native_device_identity_rejects_non_ascii_principals() -> None:
    from realtime_worker.auth import resolve_device_id

    assert resolve_device_id("device-1", None) == "device-1"
    assert resolve_device_id("device-一", None) is None


def test_lab_compatibility_can_be_disabled() -> None:
    auth = WorkerAuthenticator(worker_settings().model_copy(update={"allow_lab_auth": False}))
    assert auth.verify("Bearer lab-test-token", "device-1") is None


def test_production_and_director_lifecycle_settings_fail_closed() -> None:
    with pytest.raises(ValueError, match="ALLOW_LAB_AUTH"):
        worker_settings().model_copy(update={"environment": "production"}).validate_runtime()
    with pytest.raises(ValueError, match="HEARTBEAT_ENABLED"):
        worker_settings().model_copy(
            update={"director_url": "http://director.test", "heartbeat_enabled": False}
        ).validate_runtime()
    with pytest.raises(ValueError, match="wss://"):
        worker_settings().model_copy(
            update={
                "environment": "production",
                "allow_lab_auth": False,
                "director_url": "https://director.test",
                "worker_public_ws_url": "ws://worker.test/v1/xiaozhi",
            }
        ).validate_runtime()


@pytest.mark.parametrize(
    "public_url",
    ["wss:not-an-authority", "https://worker.test/v1/xiaozhi", "wss://worker.test/wrong"],
)
def test_worker_public_url_rejects_invalid_wire_endpoint(public_url: str) -> None:
    with pytest.raises(ValueError, match="VOICE_WORKER_PUBLIC_WS_URL"):
        worker_settings().model_copy(
            update={
                "legacy_xiaozhi_enabled": True,
                "worker_public_ws_url": public_url,
            }
        ).validate_runtime()


@pytest.mark.parametrize(
    "public_url",
    ["wss:not-an-authority", "https://worker.test/v1/voice", "wss://worker.test/wrong"],
)
def test_rva_public_url_rejects_invalid_wire_endpoint(public_url: str) -> None:
    with pytest.raises(ValueError, match="VOICE_RVA_PUBLIC_WS_URL"):
        worker_settings().model_copy(update={"rva_public_ws_url": public_url}).validate_runtime()


@pytest.mark.asyncio
async def test_worker_local_admission_defaults_to_five_and_drain_rejects_new_sessions() -> None:
    admission = SharedSessionAdmission(5)
    tokens = [await admission.reserve(("tenant-1", f"device-{index}")) for index in range(5)]
    assert all(tokens)
    assert await admission.reserve(("tenant-1", "device-6")) is None

    assert tokens[0] is not None
    await admission.release(tokens[0])
    admission.set_draining(True)
    assert await admission.reserve(("tenant-1", "device-6")) is None
