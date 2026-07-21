#pragma once

#include <cstdint>

namespace rva::board::lichuang_s3 {

struct DisplayConfig final {
    static constexpr int kWidth = 320;
    static constexpr int kHeight = 240;
    static constexpr int kSpiMosiGpio = 40;
    static constexpr int kSpiClockGpio = 41;
    static constexpr int kDisplayDcGpio = 39;
    static constexpr int kBacklightGpio = 42;
    static constexpr bool kBacklightActiveLow = true;
    static constexpr uint32_t kPixelClockHz = 80000000;
    static constexpr int kSpiMode = 2;
    static constexpr int kTransactionQueueDepth = 10;
    static constexpr bool kSwapXy = true;
    static constexpr bool kMirrorX = true;
    static constexpr bool kMirrorY = false;
    static constexpr uint8_t kPca9557DisplayResetBit = 0;
};

}  // namespace rva::board::lichuang_s3
