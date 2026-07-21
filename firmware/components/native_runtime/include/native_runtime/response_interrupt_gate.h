#pragma once

#include <cstdint>
#include <mutex>
#include <string>

#include "voice_protocol/control.h"

namespace rva::runtime {

// Serializes response lifecycle updates from the supervisor with VAD edges from uplink.
class ResponseInterruptGate final {
public:
    void Reset() {
        std::lock_guard<std::mutex> lock(mutex_);
        active_ = {};
        cancel_sent_generation_ = 0;
    }

    void Begin(const std::string& response_id, uint32_t generation) {
        std::lock_guard<std::mutex> lock(mutex_);
        active_ = {response_id, generation};
        cancel_sent_generation_ = 0;
    }

    void End() { Reset(); }

    [[nodiscard]] bool active() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return !active_.response_id.empty() && active_.generation != 0;
    }

    bool PrepareCancel(protocol::CancelTarget* target) {
        if (target == nullptr) return false;
        std::lock_guard<std::mutex> lock(mutex_);
        if (active_.response_id.empty() || active_.generation == 0 ||
            cancel_sent_generation_ == active_.generation) {
            return false;
        }
        *target = active_;
        cancel_sent_generation_ = active_.generation;
        return true;
    }

private:
    mutable std::mutex mutex_;
    protocol::CancelTarget active_{};
    uint32_t cancel_sent_generation_ = 0;
};

}  // namespace rva::runtime
