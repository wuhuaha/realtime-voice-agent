#pragma once

#include <cstdint>

namespace rva::board::lichuang_s3 {

// Values below come from the pinned, device-verified Lichuang production baseline.
struct AudioConfig final {
    static constexpr int kInputSampleRateHz = 24000;
    static constexpr int kOutputSampleRateHz = 24000;
    static constexpr int kInputChannels = 2;

    static constexpr int kI2cPort = 1;
    static constexpr int kI2cSdaGpio = 1;
    static constexpr int kI2cSclGpio = 2;
    static constexpr uint32_t kI2cFrequencyHz = 400000;

    static constexpr int kI2sPort = 0;
    static constexpr int kI2sMclkGpio = 38;
    static constexpr int kI2sBclkGpio = 14;
    static constexpr int kI2sWordSelectGpio = 13;
    static constexpr int kI2sDataInGpio = 12;
    static constexpr int kI2sDataOutGpio = 45;

    // esp_codec_dev uses the 8-bit wire address and shifts it internally;
    // ESP-IDF's i2c_device_config_t uses the corresponding 7-bit address.
    static constexpr uint8_t kEs8311CodecApiAddress8Bit = 0x30;
    static constexpr uint8_t kEs8311Address7Bit = 0x18;
    static constexpr uint8_t kEs7210CodecApiAddress8Bit = 0x82;
    static constexpr uint8_t kEs7210Address7Bit = 0x41;
    static constexpr uint8_t kPca9557Address7Bit = 0x19;
    static constexpr uint8_t kPca9557PowerAmplifierBit = 1;

    static constexpr float kNearEndInputGainDb = 37.5F;
    static constexpr float kReferenceInputGainDb = 0.0F;

    // RX is four-slot TDM. The selected stream exposed to the pipeline is MR:
    // near-end MIC1 followed by the playback reference captured on physical MIC3.
    static constexpr int kRxTdmSlots = 4;
    static constexpr int kBitsPerSample = 16;
    static constexpr int kMclkMultiple = 256;
    static constexpr int kNearEndCaptureSlot = 0;
    static constexpr int kReferenceCaptureSlot = 1;
    static constexpr int kReferencePhysicalMic = 3;
};

static_assert(AudioConfig::kInputChannels == 2);
static_assert(AudioConfig::kPca9557PowerAmplifierBit < 8);
static_assert((AudioConfig::kEs8311CodecApiAddress8Bit >> 1) ==
              AudioConfig::kEs8311Address7Bit);
static_assert((AudioConfig::kEs7210CodecApiAddress8Bit >> 1) ==
              AudioConfig::kEs7210Address7Bit);

}  // namespace rva::board::lichuang_s3
