#pragma once

#include <cstddef>
#include <cstdio>

#include "transport_udp/types.h"

namespace rva::udp {

inline bool FormatEndpointAddressForLog(
    const Endpoint& endpoint, char* output, size_t capacity) {
    if (!endpoint.valid() || output == nullptr || capacity == 0) return false;
    int written = -1;
    if (endpoint.address_bytes == 4) {
        written = std::snprintf(
            output, capacity, "%u.%u.%u.x",
            static_cast<unsigned>(endpoint.address[0]),
            static_cast<unsigned>(endpoint.address[1]),
            static_cast<unsigned>(endpoint.address[2]));
    } else {
        written = std::snprintf(
            output, capacity,
            "%02x%02x:%02x%02x:%02x%02x::/48",
            static_cast<unsigned>(endpoint.address[0]),
            static_cast<unsigned>(endpoint.address[1]),
            static_cast<unsigned>(endpoint.address[2]),
            static_cast<unsigned>(endpoint.address[3]),
            static_cast<unsigned>(endpoint.address[4]),
            static_cast<unsigned>(endpoint.address[5]));
    }
    return written > 0 && static_cast<size_t>(written) < capacity;
}

}  // namespace rva::udp
