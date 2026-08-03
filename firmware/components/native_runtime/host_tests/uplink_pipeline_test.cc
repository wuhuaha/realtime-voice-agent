#include <cassert>
#include <cstdint>
#include <deque>
#include <vector>

#include "native_runtime/uplink_pipeline.h"

namespace {

template <typename T>
class TestQueue final {
public:
    explicit TestQueue(size_t capacity) : capacity_(capacity) {}

    bool Send(const T& value) {
        if (items_.size() == capacity_) return false;
        items_.push_back(value);
        return true;
    }

    bool Receive(T* value) {
        if (items_.empty() || value == nullptr) return false;
        *value = items_.front();
        items_.pop_front();
        return true;
    }

    const std::deque<T>& items() const { return items_; }

private:
    size_t capacity_;
    std::deque<T> items_;
};

}  // namespace

int main() {
    using rva::runtime::EnqueueLatest;
    using rva::runtime::IsWssUplinkFrameFresh;
    using rva::runtime::kWssMediaSendTimeoutMs;
    using rva::runtime::LatestEnqueueResult;
    using rva::runtime::UplinkFramer;
    using rva::runtime::UplinkPcmFrame;

    assert(IsWssUplinkFrameFresh(1000, 1000));
    assert(IsWssUplinkFrameFresh(1000, 301000));
    assert(!IsWssUplinkFrameFresh(1000, 301001));
    assert(!IsWssUplinkFrameFresh(-1, 1000));
    assert(!IsWssUplinkFrameFresh(2000, 1000));
    assert(!IsWssUplinkFrameFresh(1000, 1000, -1));
    static_assert(kWssMediaSendTimeoutMs == 250);

    UplinkFramer framer;
    std::vector<int16_t> first(320, 1);
    std::vector<int16_t> second(800, 2);
    std::vector<UplinkPcmFrame> frames;
    assert(framer.Consume(first.data(), first.size(), 1000,
                          [&](const UplinkPcmFrame& frame) { frames.push_back(frame); }) == 0);
    assert(framer.remainder_samples() == 320);
    assert(framer.Consume(second.data(), second.size(), 2000,
                          [&](const UplinkPcmFrame& frame) { frames.push_back(frame); }) == 1);
    assert(frames.size() == 1);
    assert(frames[0].timestamp == 0);
    assert(frames[0].captured_at_us == 2000);
    assert(frames[0].samples[319] == 1);
    assert(frames[0].samples[320] == 2);
    assert(framer.remainder_samples() == 160);
    assert(framer.next_timestamp() == 960);

    std::vector<int16_t> multi(1760, 3);
    assert(framer.Consume(multi.data(), multi.size(), 3000,
                          [&](const UplinkPcmFrame& frame) { frames.push_back(frame); }) == 2);
    assert(frames[1].timestamp == 960);
    assert(frames[2].timestamp == 1920);
    assert(framer.remainder_samples() == 0);
    framer.Reset(4800);
    assert(framer.next_timestamp() == 4800);
    assert(framer.remainder_samples() == 0);

    TestQueue<int> queue(2);
    const auto send = [&](const int& value) { return queue.Send(value); };
    const auto receive = [&](int* value) { return queue.Receive(value); };
    assert(EnqueueLatest(1, send, receive) == LatestEnqueueResult::kEnqueued);
    assert(EnqueueLatest(2, send, receive) == LatestEnqueueResult::kEnqueued);
    assert(EnqueueLatest(3, send, receive) == LatestEnqueueResult::kReplacedOldest);
    assert(queue.items().size() == 2);
    assert(queue.items()[0] == 2);
    assert(queue.items()[1] == 3);

    TestQueue<int> unavailable(0);
    assert(EnqueueLatest(
               4,
               [&](const int& value) { return unavailable.Send(value); },
               [&](int* value) { return unavailable.Receive(value); }) ==
           LatestEnqueueResult::kFailed);
    return 0;
}
