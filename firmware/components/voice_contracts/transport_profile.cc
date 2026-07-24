#include "voice_contracts/transport_profile.h"

namespace voice::contracts {

namespace {

constexpr std::string_view kWssOpusV3 = "wss-opus-v3";
constexpr std::string_view kUdpOpusGcmV2 = "udp-opus-gcm-v2";

}  // namespace

std::string_view ToWireName(TransportProfile profile) {
    switch (profile) {
        case TransportProfile::kWssOpusV3:
            return kWssOpusV3;
        case TransportProfile::kUdpOpusGcmV2:
            return kUdpOpusGcmV2;
    }
    return {};
}

std::optional<TransportProfile> ParseTransportProfile(std::string_view wire_name) {
    if (wire_name == kWssOpusV3) {
        return TransportProfile::kWssOpusV3;
    }
    if (wire_name == kUdpOpusGcmV2) {
        return TransportProfile::kUdpOpusGcmV2;
    }
    return std::nullopt;
}

}  // namespace voice::contracts
