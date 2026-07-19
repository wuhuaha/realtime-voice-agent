#include "voice_contracts/transport_profile.h"

namespace voice::contracts {

namespace {

constexpr std::string_view kWssOpusV1 = "wss-opus-v1";
constexpr std::string_view kUdpOpusGcmV1 = "udp-opus-gcm-v1";

}  // namespace

std::string_view ToWireName(TransportProfile profile) {
    switch (profile) {
        case TransportProfile::kWssOpusV1:
            return kWssOpusV1;
        case TransportProfile::kUdpOpusGcmV1:
            return kUdpOpusGcmV1;
    }
    return {};
}

std::optional<TransportProfile> ParseTransportProfile(std::string_view wire_name) {
    if (wire_name == kWssOpusV1) {
        return TransportProfile::kWssOpusV1;
    }
    if (wire_name == kUdpOpusGcmV1) {
        return TransportProfile::kUdpOpusGcmV1;
    }
    return std::nullopt;
}

}  // namespace voice::contracts
