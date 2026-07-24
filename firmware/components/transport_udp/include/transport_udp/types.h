#pragma once

#include <array>
#include <cstddef>
#include <cstdint>

#include "voice_contracts/udp_wire.h"

namespace rva::udp {

namespace wire = voice::contracts::udp_v2;

inline constexpr size_t kAes128KeyBytes = 16;
using Aes128Key = std::array<uint8_t, kAes128KeyBytes>;

struct Endpoint final {
    std::array<uint8_t, 16> address{};
    uint8_t address_bytes = 0;
    uint16_t port = 0;

    bool operator==(const Endpoint& other) const {
        if (!valid() || !other.valid() || address_bytes != other.address_bytes ||
            port != other.port) {
            return false;
        }
        for (uint8_t index = 0; index < address_bytes; ++index) {
            if (address[index] != other.address[index]) return false;
        }
        return true;
    }
    [[nodiscard]] bool valid() const {
        return (address_bytes == 4 || address_bytes == 16) && port != 0;
    }
};

struct SessionGrant final {
    Endpoint server;
    wire::MediaId media_id{};
    uint32_t media_epoch = 0;
    uint32_t initial_downlink_generation = 1;
    Aes128Key uplink_key{};
    Aes128Key downlink_key{};
    wire::DirectionalSalt uplink_salt{};
    wire::DirectionalSalt downlink_salt{};
};

struct Stats final {
    uint32_t received = 0;
    uint32_t invalid_framing = 0;
    uint32_t wrong_session = 0;
    uint32_t authentication_failed = 0;
    uint32_t replayed = 0;
    uint32_t wrong_source = 0;
    uint32_t stale_generation = 0;
    uint32_t future_generation = 0;
    uint32_t late = 0;
    uint32_t lost = 0;
    uint32_t queue_dropped = 0;
    uint32_t played = 0;
    uint32_t max_media_age_ms = 0;
};

enum class AdmissionResult : uint8_t {
    kAcceptedAudio,
    kAcceptedProbeAck,
    kAcceptedKeepalive,
    kInvalidFraming,
    kWrongSession,
    kWrongSource,
    kReplay,
    kAuthenticationFailed,
    kStaleGeneration,
    kFutureGeneration,
    kQueueFull,
    kNotReady,
    kRevoked,
};

}  // namespace rva::udp
