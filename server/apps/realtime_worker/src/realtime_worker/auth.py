from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass
from typing import Literal

from voice_contracts import GrantCodec, GrantError

from .config import Settings

ControlProtocol = Literal["rva/1"]
TransportProfile = Literal["wss-opus/1", "udp-opus-gcm/1"]
logger = logging.getLogger(__name__)


def device_ref(tenant_id: str, device_id: str, key: str) -> str:
    message = f"{tenant_id}\0{device_id}".encode()
    return hmac.new(key.encode(), message, hashlib.sha256).hexdigest()[:12]


@dataclass(frozen=True, slots=True)
class AuthContext:
    tenant_id: str
    device_id: str
    allowed_profiles: tuple[TransportProfile, ...] = ("wss-opus/1", "udp-opus-gcm/1")
    session_epoch: str | None = None
    fencing_token: int | None = None
    expires_at: float | None = None
    control_protocol: ControlProtocol = "rva/1"


@dataclass(frozen=True, slots=True)
class VerifiedAuth:
    context: AuthContext
    director_grant: str | None


class WorkerAuthenticator:
    """Accept a Director grant or the explicit single-process lab credential."""

    def __init__(self, settings: Settings) -> None:
        self._worker_id = settings.worker_id
        self._device_ref_key = settings.internal_token.get_secret_value()
        self._rva_udp_enabled = settings.rva_udp_enabled
        self._lab_token = settings.lab_token.get_secret_value() if settings.allow_lab_auth else None
        self._codec = GrantCodec(settings.grant_signing_key.get_secret_value())

    def verify(
        self,
        authorization: str | None,
        device_id: str | None,
        *,
        control_protocol: ControlProtocol = "rva/1",
    ) -> VerifiedAuth | None:
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
                device_ref(f"worker:{self._worker_id}", device_id, self._device_ref_key),
            )
            return None
        if self._lab_token is not None and hmac.compare_digest(supplied, self._lab_token):
            profiles: tuple[TransportProfile, ...] = (
                ("wss-opus/1", "udp-opus-gcm/1")
                if self._rva_udp_enabled
                else ("wss-opus/1",)
            )
            return VerifiedAuth(
                AuthContext("lab", device_id, profiles, control_protocol=control_protocol),
                None,
            )
        try:
            claims = self._codec.verify(supplied, worker_id=self._worker_id, device_id=device_id)
        except GrantError as exc:
            logger.warning(
                "worker_auth_rejected reason=grant_%s worker_id=%s device_ref=%s token_length=%d",
                str(exc).replace(" ", "_"),
                self._worker_id,
                device_ref(f"worker:{self._worker_id}", device_id, self._device_ref_key),
                len(supplied),
            )
            return None
        if claims.control_protocol != control_protocol:
            logger.warning(
                "worker_auth_rejected reason=control_protocol_mismatch worker_id=%s device_ref=%s",
                self._worker_id,
                device_ref(f"worker:{self._worker_id}", device_id, self._device_ref_key),
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
                control_protocol=claims.control_protocol,
            ),
            supplied,
        )


def resolve_device_id(device_id: str | None, client_id: str | None) -> str | None:
    """Keep the grant-bound physical Device-Id as the stable principal."""

    def valid(value: str | None) -> bool:
        return (
            value is not None
            and 1 <= len(value) <= 96
            and value.isascii()
            and value[0].isalnum()
            and all(character.isalnum() or character in "_.:-" for character in value)
        )

    if not valid(device_id) or (client_id is not None and not valid(client_id)):
        return None
    return device_id
