#pragma once

#include <optional>
#include <string_view>

namespace voice::contracts {

enum class TransportProfile {
    kWssOpusV3,
    kUdpOpusGcmV2,
};

std::string_view ToWireName(TransportProfile profile);
std::optional<TransportProfile> ParseTransportProfile(std::string_view wire_name);

}  // namespace voice::contracts
