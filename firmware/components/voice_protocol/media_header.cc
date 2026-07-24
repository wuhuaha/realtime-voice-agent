#include "voice_protocol/media_header.h"

#include <algorithm>

namespace rva::protocol {
namespace {

constexpr uint8_t kMagic0 = 0x56;
constexpr uint8_t kMagic1 = 0x41;
constexpr uint8_t kWireVersion = 2;
constexpr uint8_t kKnownFlags = 0x01 | 0x02 | 0x04;

uint32_t ReadU32(const uint8_t* value) {
    return (static_cast<uint32_t>(value[0]) << 24) |
           (static_cast<uint32_t>(value[1]) << 16) |
           (static_cast<uint32_t>(value[2]) << 8) |
           static_cast<uint32_t>(value[3]);
}

void WriteU32(uint32_t value, uint8_t* output) {
    output[0] = static_cast<uint8_t>(value >> 24);
    output[1] = static_cast<uint8_t>(value >> 16);
    output[2] = static_cast<uint8_t>(value >> 8);
    output[3] = static_cast<uint8_t>(value);
}

MediaError Validate(const MediaHeader& header, MediaDirection direction) {
    if (header.flags == 0 || (header.flags & ~kKnownFlags) != 0 ||
        (header.flags & (header.flags - 1)) != 0) {
        return MediaError::kInvalidFlags;
    }
    if (header.media_epoch == 0) {
        return MediaError::kInvalidIdentity;
    }
    const bool audio = header.flags == 0x01;
    if ((direction == MediaDirection::kUplink && header.generation != 0) ||
        (direction == MediaDirection::kDownlink &&
         (audio ? header.generation == 0 : header.generation != 0))) {
        return MediaError::kInvalidGeneration;
    }
    if (header.payload_length > kWssMaxPayloadBytes) {
        return MediaError::kInvalidPayloadLength;
    }
    return MediaError::kOk;
}

}  // namespace

MediaError SerializeMediaHeader(
    const MediaHeader& header,
    MediaDirection direction,
    uint8_t output[kMediaHeaderBytes]) {
    if (output == nullptr) {
        return MediaError::kInvalidSize;
    }
    const MediaError error = Validate(header, direction);
    if (error != MediaError::kOk) {
        return error;
    }
    output[0] = kMagic0;
    output[1] = kMagic1;
    output[2] = kWireVersion;
    output[3] = header.flags;
    std::copy(header.media_id.begin(), header.media_id.end(), output + 4);
    WriteU32(header.media_epoch, output + 12);
    WriteU32(header.sequence, output + 16);
    WriteU32(header.timestamp, output + 20);
    WriteU32(header.generation, output + 24);
    WriteU32(header.payload_length, output + 28);
    return MediaError::kOk;
}

MediaError ParseMediaHeader(
    const uint8_t* frame,
    size_t frame_size,
    MediaDirection direction,
    MediaHeader* header) {
    if (frame == nullptr || header == nullptr || frame_size < kMediaHeaderBytes ||
        frame_size > kWssMaxFrameBytes) {
        return MediaError::kInvalidSize;
    }
    if (frame[0] != kMagic0 || frame[1] != kMagic1) {
        return MediaError::kInvalidMagic;
    }
    if (frame[2] != kWireVersion) {
        return MediaError::kUnsupportedVersion;
    }
    MediaHeader parsed;
    parsed.flags = frame[3];
    std::copy(frame + 4, frame + 12, parsed.media_id.begin());
    parsed.media_epoch = ReadU32(frame + 12);
    parsed.sequence = ReadU32(frame + 16);
    parsed.timestamp = ReadU32(frame + 20);
    parsed.generation = ReadU32(frame + 24);
    parsed.payload_length = ReadU32(frame + 28);
    const MediaError error = Validate(parsed, direction);
    if (error != MediaError::kOk) {
        return error;
    }
    if (frame_size != kMediaHeaderBytes + parsed.payload_length) {
        return MediaError::kInvalidPayloadLength;
    }
    *header = parsed;
    return MediaError::kOk;
}

}  // namespace rva::protocol
