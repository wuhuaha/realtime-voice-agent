from __future__ import annotations

from typing import Annotated, Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")]
TransportProfile = Literal["wss-opus-v1", "udp-opus-gcm-v1"]


def encode_route_key(tenant_id: str, device_id: str) -> str:
    """Return an injective, Redis-safe encoding for a tenant/device pair."""

    return f"{quote(tenant_id, safe='')}:{quote(device_id, safe='')}"


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkerHeartbeat(ContractModel):
    worker_id: Identifier
    public_wss_url: str
    active_sessions: int = Field(ge=0)
    max_sessions: int = Field(default=5, ge=1, le=1024)
    draining: bool = False
    healthy: bool = True
    profiles: tuple[TransportProfile, ...] = ("wss-opus-v1",)
    active_leases: tuple[LeaseRenewal, ...] = ()
    released_leases: tuple[LeaseRenewal, ...] = Field(default=(), max_length=64)

    @field_validator("profiles")
    @classmethod
    def unique_profiles(cls, value: tuple[TransportProfile, ...]) -> tuple[TransportProfile, ...]:
        if not value or len(value) != len(set(value)):
            raise ValueError("profiles must be non-empty and unique")
        return value

    @model_validator(mode="after")
    def bounded_active_leases(self) -> WorkerHeartbeat:
        if len(self.active_leases) > self.max_sessions:
            raise ValueError("active_leases cannot exceed max_sessions")
        principals = {(lease.tenant_id, lease.device_id) for lease in self.active_leases}
        if len(principals) != len(self.active_leases):
            raise ValueError("active_leases must contain unique principals")
        released = {(lease.tenant_id, lease.device_id, lease.session_epoch) for lease in self.released_leases}
        if len(released) != len(self.released_leases):
            raise ValueError("released_leases must be unique")
        return self


class WorkerSnapshot(WorkerHeartbeat):
    heartbeat_expires_at: float

    @property
    def available_slots(self) -> int:
        return max(0, self.max_sessions - self.active_sessions)


class WorkerHeartbeatResponse(ContractModel):
    accepted: bool = True
    draining: bool
    heartbeat_expires_at: float
    lease_expires_at: float
    rejected_session_epochs: tuple[Identifier, ...] = ()


class RouteLease(ContractModel):
    tenant_id: Identifier
    device_id: Identifier
    worker_id: Identifier
    session_epoch: Identifier
    fencing_token: int = Field(ge=1)
    expires_at: float

    @property
    def route_key(self) -> str:
        return encode_route_key(self.tenant_id, self.device_id)


class LeaseRenewal(ContractModel):
    tenant_id: Identifier
    device_id: Identifier
    session_epoch: Identifier
    fencing_token: int = Field(ge=1)


class ConnectGrantClaims(ContractModel):
    iss: Literal["session_director"] = "session_director"
    aud: Literal["realtime_worker"] = "realtime_worker"
    tenant_id: Identifier
    device_id: Identifier
    worker_id: Identifier
    session_epoch: Identifier
    fencing_token: int = Field(ge=1)
    profiles: tuple[TransportProfile, ...]
    iat: float
    exp: float
    jti: Identifier

    @field_validator("profiles")
    @classmethod
    def validate_profiles(cls, value: tuple[TransportProfile, ...]) -> tuple[TransportProfile, ...]:
        if not value or len(value) != len(set(value)):
            raise ValueError("profiles must be non-empty and unique")
        return value


class BootstrapRequest(ContractModel):
    tenant_id: Identifier = "default"
    device_id: Identifier
    supported_profiles: tuple[TransportProfile, ...] = ("wss-opus-v1",)


class BootstrapResponse(ContractModel):
    worker_id: Identifier
    worker_wss_url: str
    connect_grant: str
    session_epoch: Identifier
    fencing_token: int
    allowed_profiles: tuple[TransportProfile, ...]
    expires_at: float


class DrainRequest(ContractModel):
    draining: bool = True


class GrantConsumeRequest(ContractModel):
    token: str = Field(min_length=32, max_length=4096)
    worker_id: Identifier
    device_id: Identifier


class GrantConsumeResponse(ContractModel):
    consumed: Literal[True] = True
    session_epoch: Identifier
    fencing_token: int = Field(ge=1)
    expires_at: float
