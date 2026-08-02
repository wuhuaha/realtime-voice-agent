from __future__ import annotations

import ipaddress
import math
import re
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import urlsplit

_BOOTSTRAP_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")


def _loopback_host(host: str | None) -> bool:
    if host is None:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class MediaProfile(StrEnum):
    WSS_OPUS_V1 = "wss-opus/1"
    UDP_OPUS_GCM_V1 = "udp-opus-gcm/1"


@dataclass(frozen=True, slots=True)
class EndpointCapabilities:
    aec: bool = False
    vad: bool = False
    wake_word: bool = False
    display: bool = True
    touch: bool = False

    def __post_init__(self) -> None:
        if any(type(value) is not bool for value in (self.aec, self.vad, self.wake_word, self.display, self.touch)):
            raise ValueError("capability values must have bool runtime types")

    def as_wire(self) -> dict[str, bool]:
        return {
            "aec": self.aec,
            "vad": self.vad,
            "wake_word": self.wake_word,
            "display": self.display,
            "touch": self.touch,
        }


@dataclass(frozen=True, slots=True)
class ClientConfig:
    director_url: str
    bootstrap_token: str = field(repr=False)
    device_id: str
    tenant_id: str = "default"
    supported_profiles: tuple[MediaProfile, ...] = (
        MediaProfile.UDP_OPUS_GCM_V1,
        MediaProfile.WSS_OPUS_V1,
    )
    preferred_profile: MediaProfile = MediaProfile.UDP_OPUS_GCM_V1
    capabilities: EndpointCapabilities = EndpointCapabilities()
    connect_timeout_seconds: float = 5.0
    control_timeout_seconds: float = 5.0
    media_max_age_seconds: float = 0.12
    udp_probe_retry_seconds: float = 0.2
    allow_insecure_loopback: bool = False

    def __post_init__(self) -> None:
        parsed = urlsplit(self.director_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("director_url must be an absolute HTTP(S) URL")
        if parsed.scheme == "http" and not (self.allow_insecure_loopback and _loopback_host(parsed.hostname)):
            raise ValueError("plain HTTP Director is allowed only for an explicitly enabled loopback environment")
        if not self.bootstrap_token:
            raise ValueError("bootstrap_token must not be empty")
        if _BOOTSTRAP_IDENTIFIER.fullmatch(self.device_id) is None:
            raise ValueError("device_id is not a valid bootstrap identifier")
        if _BOOTSTRAP_IDENTIFIER.fullmatch(self.tenant_id) is None:
            raise ValueError("tenant_id is not a valid bootstrap identifier")
        if type(self.supported_profiles) is not tuple or not all(
            type(profile) is MediaProfile for profile in self.supported_profiles
        ):
            raise ValueError("supported_profiles must have tuple[MediaProfile, ...] runtime types")
        if type(self.preferred_profile) is not MediaProfile:
            raise ValueError("preferred_profile must have MediaProfile runtime type")
        if type(self.capabilities) is not EndpointCapabilities:
            raise ValueError("capabilities must have EndpointCapabilities runtime type")
        if not self.supported_profiles or len(set(self.supported_profiles)) != len(self.supported_profiles):
            raise ValueError("supported_profiles must be non-empty and unique")
        if self.preferred_profile not in self.supported_profiles:
            raise ValueError("preferred_profile must be supported")
        durations = (
            self.connect_timeout_seconds,
            self.control_timeout_seconds,
            self.media_max_age_seconds,
            self.udp_probe_retry_seconds,
        )
        if any(type(value) not in {int, float} or not math.isfinite(value) or value <= 0 for value in durations):
            raise ValueError("duration values must be finite positive numbers")
