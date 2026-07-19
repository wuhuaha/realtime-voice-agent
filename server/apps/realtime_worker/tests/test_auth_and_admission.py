from __future__ import annotations

import time

import pytest
from realtime_worker.auth import WorkerAuthenticator
from realtime_worker.bindings.xiaozhi import SharedSessionAdmission
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
) -> str:
    now = time.time()
    claims = ConnectGrantClaims(
        tenant_id="tenant-1",
        device_id="device-1",
        worker_id=worker_id,
        session_epoch=session_epoch,
        fencing_token=fencing_token,
        profiles=("wss-opus-v1",),
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
    assert accepted.context.allowed_profiles == ("wss-opus-v1",)
    assert accepted.context.expires_at is not None
    assert accepted.director_grant == token
    assert WorkerAuthenticator(worker_settings("worker-b")).verify(f"Bearer {grant()}", "device-1") is None


def test_expired_and_wrong_device_grants_fail_closed() -> None:
    auth = WorkerAuthenticator(worker_settings())
    assert auth.verify(f"Bearer {grant(exp=time.time() - 1, jti='expired')}", "device-1") is None
    assert auth.verify(f"Bearer {grant(jti='wrong-device')}", "device-2") is None


def test_lab_compatibility_does_not_require_director_consumption() -> None:
    auth = WorkerAuthenticator(worker_settings())
    accepted = auth.verify("Bearer lab-test-token", "device-1")
    assert accepted is not None
    assert accepted.director_grant is None
    assert accepted.context.expires_at is None


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
