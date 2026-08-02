#include "voice_contracts/udp_wire.h"

#include <algorithm>

namespace voice::contracts::udp_v1 {

namespace {

std::uint32_t ReadU32(const std::uint8_t* bytes) {
    return (static_cast<std::uint32_t>(bytes[0]) << 24) |
           (static_cast<std::uint32_t>(bytes[1]) << 16) |
           (static_cast<std::uint32_t>(bytes[2]) << 8) |
           static_cast<std::uint32_t>(bytes[3]);
}

void WriteU32(std::uint8_t* bytes, std::uint32_t value) {
    bytes[0] = static_cast<std::uint8_t>(value >> 24);
    bytes[1] = static_cast<std::uint8_t>(value >> 16);
    bytes[2] = static_cast<std::uint8_t>(value >> 8);
    bytes[3] = static_cast<std::uint8_t>(value);
}

std::optional<DatagramType> ParseType(std::uint8_t value) {
    switch (value) {
        case static_cast<std::uint8_t>(DatagramType::kAudio):
            return DatagramType::kAudio;
        case static_cast<std::uint8_t>(DatagramType::kProbe):
            return DatagramType::kProbe;
        case static_cast<std::uint8_t>(DatagramType::kProbeAck):
            return DatagramType::kProbeAck;
        case static_cast<std::uint8_t>(DatagramType::kKeepalive):
            return DatagramType::kKeepalive;
        default:
            return std::nullopt;
    }
}

bool HeaderShapeValid(const Header& header, Direction direction) {
    if (header.media_epoch == 0 || header.payload_length > kMaxPayloadBytes) {
        return false;
    }
    switch (header.type) {
        case DatagramType::kAudio:
            return header.payload_length > 0;
        case DatagramType::kProbe:
            return direction == Direction::kUplink &&
                   header.payload_length == 0 && header.timestamp == 0;
        case DatagramType::kProbeAck:
            return direction == Direction::kDownlink &&
                   header.payload_length == 0 && header.timestamp == 0;
        case DatagramType::kKeepalive:
            return header.payload_length == 0;
    }
    return false;
}

}  // namespace

bool HeaderFieldsValid(const Header& header, Direction direction) {
    if (!HeaderShapeValid(header, direction)) return false;
    const bool audio = header.type == DatagramType::kAudio;
    if ((direction == Direction::kUplink && header.generation != 0) ||
        (direction == Direction::kDownlink &&
         (audio ? header.generation == 0 : header.generation != 0))) {
        return false;
    }
    return true;
}

WireHeader EncodeHeader(const Header& header) {
    WireHeader bytes{};
    bytes[0] = 'V';
    bytes[1] = 'A';
    bytes[2] = kVersion;
    bytes[3] = static_cast<std::uint8_t>(header.type);
    std::copy(header.media_id.begin(), header.media_id.end(), bytes.begin() + 4);
    WriteU32(bytes.data() + 12, header.media_epoch);
    WriteU32(bytes.data() + 16, header.sequence);
    WriteU32(bytes.data() + 20, header.timestamp);
    WriteU32(bytes.data() + 24, header.generation);
    WriteU32(bytes.data() + 28, header.payload_length);
    return bytes;
}

std::optional<DatagramView> ParseDatagram(
    const std::uint8_t* data, std::size_t size, Direction direction) {
    if (data == nullptr || size < kHeaderBytes + kTagBytes ||
        size > kMaxDatagramBytes || data[0] != 'V' || data[1] != 'A' ||
        data[2] != kVersion) {
        return std::nullopt;
    }
    const auto type = ParseType(data[3]);
    if (!type) {
        return std::nullopt;
    }

    Header header;
    header.type = *type;
    std::copy(data + 4, data + 12, header.media_id.begin());
    header.media_epoch = ReadU32(data + 12);
    header.sequence = ReadU32(data + 16);
    header.timestamp = ReadU32(data + 20);
    header.generation = ReadU32(data + 24);
    header.payload_length = ReadU32(data + 28);
    // Generation is authenticated policy, not framing. Keeping it out of the
    // pre-auth parser ensures a tampered AAD byte cannot bypass GCM admission.
    if (!HeaderShapeValid(header, direction) ||
        size != kHeaderBytes + header.payload_length + kTagBytes) {
        return std::nullopt;
    }

    DatagramView view;
    view.header = header;
    view.header_bytes = data;
    view.ciphertext = data + kHeaderBytes;
    view.tag = view.ciphertext + header.payload_length;
    return view;
}

bool MatchesSession(
    const Header& header, const MediaId& media_id, std::uint32_t media_epoch) {
    return media_epoch != 0 && header.media_id == media_id &&
           header.media_epoch == media_epoch;
}

Nonce MakeNonce(const DirectionalSalt& salt, std::uint32_t sequence) {
    Nonce nonce{};
    std::copy(salt.begin(), salt.end(), nonce.begin());
    WriteU32(nonce.data() + kSaltBytes, sequence);
    return nonce;
}

}  // namespace voice::contracts::udp_v1
