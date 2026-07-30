"""RVA desktop reference endpoint protocol and session core."""

from .config import ClientConfig, EndpointCapabilities, MediaProfile
from .events import SessionEvent
from .session import DesktopSession

__all__ = [
    "ClientConfig",
    "DesktopSession",
    "EndpointCapabilities",
    "MediaProfile",
    "SessionEvent",
]
