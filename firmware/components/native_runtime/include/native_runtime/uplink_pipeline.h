#pragma once

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <type_traits>

namespace rva::runtime {

inline constexpr size_t kUplinkSamplesPerFrame = 960;
inline constexpr int64_t kWssUplinkPreSendMaxAgeUs = 300000;
inline constexpr uint32_t kWssMediaSendTimeoutMs = 250;

// Leave enough of the server's 600 ms freshness budget for the bounded WSS
// send and server-side decode/dispatch work. Invalid monotonic timestamps fail
// closed so a corrupt frame cannot be admitted as current audio.
[[nodiscard]] constexpr bool IsWssUplinkFrameFresh(
    int64_t captured_at_us, int64_t now_us,
    int64_t maximum_age_us = kWssUplinkPreSendMaxAgeUs) {
    return captured_at_us >= 0 && now_us >= captured_at_us && maximum_age_us >= 0 &&
           now_us - captured_at_us <= maximum_age_us;
}

struct UplinkPcmFrame final {
    std::array<int16_t, kUplinkSamplesPerFrame> samples{};
    uint32_t timestamp = 0;
    int64_t captured_at_us = 0;
};

static_assert(std::is_trivially_copyable_v<UplinkPcmFrame>);

// Forms codec-sized frames while preserving the media clock across arbitrary
// AFE fetch sizes. A rejected frame is still consumed, so downstream drops
// create an explicit timestamp gap instead of making old audio look current.
class UplinkFramer final {
public:
    template <typename Sink>
    size_t Consume(const int16_t* samples, size_t sample_count,
                   int64_t captured_at_us, Sink&& sink) {
        if (samples == nullptr && sample_count != 0) return 0;
        size_t emitted = 0;
        size_t offset = 0;
        while (offset < sample_count) {
            const size_t copied = std::min(
                frame_.samples.size() - accumulated_samples_, sample_count - offset);
            std::copy_n(samples + offset, copied,
                        frame_.samples.begin() + accumulated_samples_);
            accumulated_samples_ += copied;
            offset += copied;
            if (accumulated_samples_ != frame_.samples.size()) continue;

            frame_.timestamp = next_timestamp_;
            frame_.captured_at_us = captured_at_us;
            sink(frame_);
            ++emitted;
            next_timestamp_ += static_cast<uint32_t>(frame_.samples.size());
            accumulated_samples_ = 0;
        }
        return emitted;
    }

    void Reset(uint32_t timestamp = 0) {
        accumulated_samples_ = 0;
        next_timestamp_ = timestamp;
    }

    [[nodiscard]] size_t remainder_samples() const { return accumulated_samples_; }
    [[nodiscard]] uint32_t next_timestamp() const { return next_timestamp_; }

private:
    UplinkPcmFrame frame_{};
    size_t accumulated_samples_ = 0;
    uint32_t next_timestamp_ = 0;
};

enum class LatestEnqueueResult : uint8_t {
    kEnqueued,
    kReplacedOldest,
    kFailed,
};

// Queue operations are injected so this exact full-queue policy is host-testable
// while the target binds it to xQueueSend/xQueueReceive. The second send handles
// the benign race where the sole consumer drains between the first send and pop.
template <typename Item, typename TrySend, typename TryReceive>
LatestEnqueueResult EnqueueLatest(
    const Item& item, TrySend&& try_send, TryReceive&& try_receive) {
    if (try_send(item)) return LatestEnqueueResult::kEnqueued;
    Item discarded{};
    if (try_receive(&discarded)) {
        return try_send(item) ? LatestEnqueueResult::kReplacedOldest
                              : LatestEnqueueResult::kFailed;
    }
    return try_send(item) ? LatestEnqueueResult::kEnqueued
                          : LatestEnqueueResult::kFailed;
}

}  // namespace rva::runtime
