#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <mutex>

#include "transport_udp/jitter_buffer.h"

namespace rva::udp {

enum class PlayoutPushResult : uint8_t { kAccepted, kFull, kClosed, kStale };

// Fixed-lifetime queue that atomically couples queue contents to the playback
// generation. It is safe for one producer, one consumer and a supervisor.
class PlayoutQueue final {
public:
    static constexpr size_t kCapacity = 4;

    void Open(uint32_t generation);
    void AdvanceGeneration(uint32_t generation);
    void Close();
    PlayoutPushResult Push(const PlayoutFrame& frame);
    bool Pop(PlayoutFrame* frame);
    [[nodiscard]] size_t size() const;

private:
    static void ClearFrame(PlayoutFrame* frame);
    void ClearLocked();

    mutable std::mutex mutex_;
    std::array<PlayoutFrame, kCapacity> items_{};
    size_t head_ = 0;
    size_t size_ = 0;
    uint32_t generation_ = 0;
    bool open_ = false;
};

}  // namespace rva::udp
