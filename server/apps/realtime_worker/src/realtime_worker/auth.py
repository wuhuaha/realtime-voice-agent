from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass
from typing import Literal

from voice_contracts import GrantCodec, GrantError

from .config import Settings

TransportProfile = Literal["wss-opus-v1", "udp-opus-gcm-v1"]
logger = logging.getLogger(__name__)


def _device_ref(device_id: str) -> str:
    return hashlib.sha256(device_id.encode("utf-8")).hexdigest()[:12]


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
        if authorization is None:
            logger.warning("worker_auth_rejected reason=missing_authorization worker_id=%s", self._worker_id)
            return None
        if device_id is None:
            logger.warning("worker_auth_rejected reason=missing_device_id worker_id=%s", self._worker_id)
            return None
        scheme, _, supplied = authorization.partition(" ")
        if scheme.lower() != "bearer":
            logger.warning(
                "worker_auth_rejected reason=invalid_scheme worker_id=%s device_ref=%s",
                self._worker_id,
                _device_ref(device_id),
            )
            return None
        if self._lab_token is not None and hmac.compare_digest(supplied, self._lab_token):
            return VerifiedAuth(
                AuthContext("lab", device_id, ("wss-opus-v1", "udp-opus-gcm-v1")),
                None,
            )
        try:
            claims = self._codec.verify(supplied, worker_id=self._worker_id, device_id=device_id)
        except GrantError as exc:
            logger.warning(
                "worker_auth_rejected reason=grant_%s worker_id=%s device_ref=%s token_length=%d",
                str(exc).replace(" ", "_"),
                self._worker_id,
                _device_ref(device_id),
                len(supplied),
            )
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
