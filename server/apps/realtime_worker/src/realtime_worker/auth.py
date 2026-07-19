from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Literal

from voice_contracts import GrantCodec, GrantError

from .config import Settings

TransportProfile = Literal["wss-opus-v1", "udp-opus-gcm-v1"]


@dataclass(frozen=True, slots=True)
class AuthContext:
    tenant_id: str
    device_id: str
    allowed_profiles: tuple[TransportProfile, ...] = ("wss-opus-v1", "udp-opus-gcm-v1")
    session_epoch: str | None = None
    fencing_token: int | None = None
    expires_at: float | None = None


@dataclass(frozen=True, slots=True)
class VerifiedAuth:
    context: AuthContext
    director_grant: str | None


class WorkerAuthenticator:
    """Accept a Director grant or the explicit single-process lab credential."""

    def __init__(self, settings: Settings) -> None:
        self._worker_id = settings.worker_id
        self._lab_token = settings.lab_token.get_secret_value() if settings.allow_lab_auth else None
        self._codec = GrantCodec(settings.grant_signing_key.get_secret_value())

    def verify(self, authorization: str | None, device_id: str | None) -> VerifiedAuth | None:
        if authorization is None or device_id is None:
            return None
        scheme, _, supplied = authorization.partition(" ")
        if scheme.lower() != "bearer":
            return None
        if self._lab_token is not None and hmac.compare_digest(supplied, self._lab_token):
            return VerifiedAuth(
                AuthContext("lab", device_id, ("wss-opus-v1", "udp-opus-gcm-v1")),
                None,
            )
        try:
            claims = self._codec.verify(supplied, worker_id=self._worker_id, device_id=device_id)
        except GrantError:
            return None
        return VerifiedAuth(
            AuthContext(
                tenant_id=claims.tenant_id,
                device_id=claims.device_id,
                allowed_profiles=claims.profiles,
                session_epoch=claims.session_epoch,
                fencing_token=claims.fencing_token,
                expires_at=claims.exp,
            ),
            supplied,
        )
