#pragma once

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>

namespace voice_udp_wire {

constexpr std::size_t kHeaderBytes = 32;
constexpr std::size_t kMediaIdBytes = 8;
constexpr std::size_t kSaltBytes = 8;
constexpr std::size_t kNonceBytes = 12;
constexpr std::size_t kTagBytes = 16;
constexpr std::size_t kMaxDatagramBytes = 1280;
constexpr std::size_t kMaxPayloadBytes = 1200;
constexpr std::uint8_t kVersion = 1;
constexpr std::uint8_t kAudio = 0x01;
constexpr std::uint8_t kProbe = 0x02;
constexpr std::uint8_t kProbeAck = 0x04;
constexpr std::uint8_t kKeepalive = 0x08;

enum class Direction {
    kUplink,
    kDownlink,
};

struct Header {
    std::uint8_t flags = 0;
    std::array<std::uint8_t, kMediaIdBytes> media_id{};
    std::uint32_t media_epoch = 0;
    std::uint32_t sequence = 0;
    std::uint32_t timestamp = 0;
    std::uint32_t generation = 0;
    std::uint32_t payload_length = 0;
};

struct DatagramView {
    Header header;
    const std::uint8_t* header_bytes = nullptr;
    const std::uint8_t* ciphertext = nullptr;
    const std::uint8_t* tag = nullptr;
};

inline std::uint32_t ReadU32(const std::uint8_t* bytes) {
    return (static_cast<std::uint32_t>(bytes[0]) << 24) |
           (static_cast<std::uint32_t>(bytes[1]) << 16) |
           (static_cast<std::uint32_t>(bytes[2]) << 8) |
           static_cast<std::uint32_t>(bytes[3]);
}

inline void WriteU32(std::uint8_t* bytes, std::uint32_t value) {
    bytes[0] = static_cast<std::uint8_t>(value >> 24);
    bytes[1] = static_cast<std::uint8_t>(value >> 16);
    bytes[2] = static_cast<std::uint8_t>(value >> 8);
    bytes[3] = static_cast<std::uint8_t>(value);
}

inline bool HeaderFieldsValid(const Header& header, Direction direction) {
    if (header.media_epoch == 0 || header.payload_length > kMaxPayloadBytes) {
        return false;
    }
    if (header.flags == kAudio) {
        return header.payload_length > 0;
    }
    if (header.flags == kKeepalive) {
        return header.payload_length == 0;
    }
    const std::uint8_t expected_probe =
        direction == Direction::kUplink ? kProbe : kProbeAck;
    return header.flags == expected_probe && header.payload_length == 0 &&
           header.timestamp == 0;
}

inline std::array<std::uint8_t, kHeaderBytes> EncodeHeader(const Header& header) {
    std::array<std::uint8_t, kHeaderBytes> bytes{};
    bytes[0] = 'V';
    bytes[1] = 'A';
    bytes[2] = kVersion;
    bytes[3] = header.flags;
    std::copy(header.media_id.begin(), header.media_id.end(), bytes.begin() + 4);
    WriteU32(bytes.data() + 12, header.media_epoch);
    WriteU32(bytes.data() + 16, header.sequence);
    WriteU32(bytes.data() + 20, header.timestamp);
    WriteU32(bytes.data() + 24, header.generation);
    WriteU32(bytes.data() + 28, header.payload_length);
    return bytes;
}

inline bool ParseDatagram(const std::uint8_t* data, std::size_t size,
                          Direction direction, DatagramView& view) {
    if (data == nullptr || size < kHeaderBytes + kTagBytes ||
        size > kMaxDatagramBytes || data[0] != 'V' || data[1] != 'A' ||
        data[2] != kVersion) {
        return false;
    }
    Header header;
    header.flags = data[3];
    std::copy(data + 4, data + 12, header.media_id.begin());
    header.media_epoch = ReadU32(data + 12);
    header.sequence = ReadU32(data + 16);
    header.timestamp = ReadU32(data + 20);
    header.generation = ReadU32(data + 24);
    header.payload_length = ReadU32(data + 28);
    if (size != kHeaderBytes + header.payload_length + kTagBytes ||
        !HeaderFieldsValid(header, direction)) {
        return false;
    }
    view.header = header;
    view.header_bytes = data;
    view.ciphertext = data + kHeaderBytes;
    view.tag = view.ciphertext + header.payload_length;
    return true;
}

inline bool MatchesSession(const Header& header,
                           const std::array<std::uint8_t, kMediaIdBytes>& media_id,
                           std::uint32_t media_epoch) {
    return header.media_id == media_id && header.media_epoch == media_epoch;
}

inline std::array<std::uint8_t, kNonceBytes> MakeNonce(
    const std::array<std::uint8_t, kSaltBytes>& salt, std::uint32_t sequence) {
    std::array<std::uint8_t, kNonceBytes> nonce{};
    std::copy(salt.begin(), salt.end(), nonce.begin());
    WriteU32(nonce.data() + kSaltBytes, sequence);
    return nonce;
}

}  // namespace voice_udp_wire
