#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <optional>

namespace voice::contracts::udp_v1 {

constexpr std::size_t kHeaderBytes = 32;
constexpr std::size_t kMediaIdBytes = 8;
constexpr std::size_t kSaltBytes = 8;
constexpr std::size_t kNonceBytes = 12;
constexpr std::size_t kTagBytes = 16;
constexpr std::size_t kMaxDatagramBytes = 1280;
constexpr std::size_t kMaxPayloadBytes = 1200;
constexpr std::uint8_t kVersion = 1;

using MediaId = std::array<std::uint8_t, kMediaIdBytes>;
using DirectionalSalt = std::array<std::uint8_t, kSaltBytes>;
using WireHeader = std::array<std::uint8_t, kHeaderBytes>;
using Nonce = std::array<std::uint8_t, kNonceBytes>;

enum class Direction {
    kUplink,
    kDownlink,
};

enum class DatagramType : std::uint8_t {
    kAudio = 0x01,
    kProbe = 0x02,
    kProbeAck = 0x04,
    kKeepalive = 0x08,
};

struct Header {
    DatagramType type = DatagramType::kAudio;
    MediaId media_id{};
    std::uint32_t media_epoch = 0;
    std::uint32_t sequence = 0;
    std::uint32_t timestamp = 0;
    std::uint32_t generation = 0;
    std::uint32_t payload_length = 0;
};

// The byte pointers borrow the input buffer passed to ParseDatagram.
struct DatagramView {
    Header header;
    const std::uint8_t* header_bytes = nullptr;
    const std::uint8_t* ciphertext = nullptr;
    const std::uint8_t* tag = nullptr;
};

bool HeaderFieldsValid(const Header& header, Direction direction);
WireHeader EncodeHeader(const Header& header);
std::optional<DatagramView> ParseDatagram(
    const std::uint8_t* data, std::size_t size, Direction direction);
bool MatchesSession(
    const Header& header, const MediaId& media_id, std::uint32_t media_epoch);
Nonce MakeNonce(const DirectionalSalt& salt, std::uint32_t sequence);

}  // namespace voice::contracts::udp_v1
