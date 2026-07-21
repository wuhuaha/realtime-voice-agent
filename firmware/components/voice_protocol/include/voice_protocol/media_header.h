#pragma once

#include <array>
#include <cstddef>
#include <cstdint>

namespace rva::protocol {

inline constexpr size_t kMediaHeaderBytes = 32;
inline constexpr size_t kWssMaxPayloadBytes = 1200;
inline constexpr size_t kWssMaxFrameBytes = kMediaHeaderBytes + kWssMaxPayloadBytes;

enum class MediaDirection {
    kUplink,
    kDownlink,
};

enum class MediaError {
    kOk = 0,
    kInvalidSize,
    kInvalidMagic,
    kUnsupportedVersion,
    kInvalidFlags,
    kInvalidIdentity,
    kInvalidGeneration,
    kInvalidPayloadLength,
};

struct MediaHeader final {
    uint8_t flags = 0;
    std::array<uint8_t, 8> media_id{};
    uint32_t media_epoch = 0;
    uint32_t sequence = 0;
    uint32_t timestamp = 0;
    uint32_t generation = 0;
    uint32_t payload_length = 0;
};

MediaError SerializeMediaHeader(
    const MediaHeader& header,
    MediaDirection direction,
    uint8_t output[kMediaHeaderBytes]);
MediaError ParseMediaHeader(
    const uint8_t* frame,
    size_t frame_size,
    MediaDirection direction,
    MediaHeader* header);

}  // namespace rva::protocol
