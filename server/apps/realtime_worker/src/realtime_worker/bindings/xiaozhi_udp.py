"""Legacy import compatibility for the Xiaozhi binding.

The UDP wire and gateway are shared transport infrastructure. New code must
import them from :mod:`realtime_worker.transport`.
"""

from ..transport.udp_gateway import (
    UDP_FLAG_AUDIO,
    UDP_FLAG_KEEPALIVE,
    UDP_FLAG_PROBE,
    UDP_FLAG_PROBE_ACK,
    UDP_HEADER_BYTES,
    UDP_KEY_BYTES,
    UDP_MAX_DATAGRAM_BYTES,
    UDP_MAX_PAYLOAD_BYTES,
    UDP_SALT_BYTES,
    UDP_TAG_BYTES,
    ReplayWindow,
    UdpGrant,
    UdpMediaError,
    UdpMediaGateway,
    UdpMediaSession,
    UdpMediaStats,
    UdpPacketHeader,
)
from ..transport.udp_wire import UDP_MAX_SEQUENCE_FORWARD_JUMP

__all__ = [
    "UDP_FLAG_AUDIO",
    "UDP_FLAG_KEEPALIVE",
    "UDP_FLAG_PROBE",
    "UDP_FLAG_PROBE_ACK",
    "UDP_HEADER_BYTES",
    "UDP_KEY_BYTES",
    "UDP_MAX_DATAGRAM_BYTES",
    "UDP_MAX_PAYLOAD_BYTES",
    "UDP_MAX_SEQUENCE_FORWARD_JUMP",
    "UDP_SALT_BYTES",
    "UDP_TAG_BYTES",
    "ReplayWindow",
    "UdpGrant",
    "UdpMediaError",
    "UdpMediaGateway",
    "UdpMediaSession",
    "UdpMediaStats",
    "UdpPacketHeader",
]
