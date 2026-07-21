#pragma once

#include <cstddef>
#include <cstdint>

namespace rva::audio {

enum class PortResult : uint8_t {
    kOk = 0,
    kInvalidArgument,
    kInvalidState,
    kResourceExhausted,
    kTimeout,
    kIoFailure,
    kInternalFailure,
};

struct PcmView final {
    const int16_t* samples = nullptr;
    size_t sample_count = 0;
    uint32_t sample_rate_hz = 0;
    uint8_t channel_count = 0;
};

struct MutablePcmView final {
    int16_t* samples = nullptr;
    size_t capacity_samples = 0;
    size_t sample_count = 0;
    uint32_t sample_rate_hz = 0;
    uint8_t channel_count = 0;
};

class CapturePort {
public:
    virtual ~CapturePort() = default;
    virtual PortResult Start() = 0;
    virtual PortResult Stop() = 0;
    virtual PortResult Read(MutablePcmView* destination, uint32_t timeout_ms) = 0;
};

class FrontendPort {
public:
    virtual ~FrontendPort() = default;
    virtual PortResult Start() = 0;
    virtual PortResult Stop() = 0;
    virtual PortResult Feed(PcmView input) = 0;
    virtual PortResult Fetch(MutablePcmView* output, uint32_t timeout_ms) = 0;
};

class PlaybackPort {
public:
    virtual ~PlaybackPort() = default;
    virtual PortResult Start() = 0;
    virtual PortResult Stop() = 0;
    virtual PortResult Write(PcmView input, uint32_t timeout_ms) = 0;
};

}  // namespace rva::audio
