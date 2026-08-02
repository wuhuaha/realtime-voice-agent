#include "transport_udp/playout_queue.h"

namespace rva::udp {

void PlayoutQueue::Open(uint32_t generation) {
    std::lock_guard<std::mutex> lock(mutex_);
    ClearLocked();
    generation_ = generation;
    open_ = generation != 0;
}

void PlayoutQueue::AdvanceGeneration(uint32_t generation) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (!open_ || generation <= generation_) return;
    ClearLocked();
    generation_ = generation;
}

void PlayoutQueue::Close() {
    std::lock_guard<std::mutex> lock(mutex_);
    open_ = false;
    generation_ = 0;
    ClearLocked();
}

PlayoutPushResult PlayoutQueue::Push(const PlayoutFrame& frame) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (!open_) return PlayoutPushResult::kClosed;
    if (frame.generation != generation_) return PlayoutPushResult::kStale;
    bool replaced = false;
    if (size_ == kCapacity) {
        ClearFrame(&items_[head_]);
        head_ = (head_ + 1) % kCapacity;
        size_--;
        replaced = true;
    }
    items_[(head_ + size_) % kCapacity] = frame;
    size_++;
    return replaced ? PlayoutPushResult::kReplacedOldest
                    : PlayoutPushResult::kAccepted;
}

bool PlayoutQueue::Pop(PlayoutFrame* frame) {
    return PopFresh(frame, 0, 0, nullptr);
}

bool PlayoutQueue::PopFresh(
    PlayoutFrame* frame, int64_t now_us, int64_t maximum_age_us,
    uint32_t* expired_count) {
    if (frame == nullptr) return false;
    if (expired_count != nullptr) *expired_count = 0;
    std::lock_guard<std::mutex> lock(mutex_);
    while (open_ && size_ > 0) {
        PlayoutFrame& candidate = items_[head_];
        *frame = candidate;
        ClearFrame(&candidate);
        head_ = (head_ + 1) % kCapacity;
        size_--;
        if (frame->generation != generation_) continue;
        const bool expired = maximum_age_us > 0 && now_us >= frame->arrived_us &&
                             now_us - frame->arrived_us > maximum_age_us;
        if (expired) {
            if (expired_count != nullptr) (*expired_count)++;
            ClearFrame(frame);
            continue;
        }
        return true;
    }
    return false;
}

size_t PlayoutQueue::size() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return size_;
}

void PlayoutQueue::ClearLocked() {
    for (auto& item : items_) ClearFrame(&item);
    head_ = 0;
    size_ = 0;
}

void PlayoutQueue::ClearFrame(PlayoutFrame* frame) {
    volatile uint8_t* bytes = frame->payload.data();
    for (size_t index = 0; index < frame->payload.size(); ++index) bytes[index] = 0;
    *frame = {};
}

}  // namespace rva::udp
