#include <atomic>
#include <cassert>
#include <thread>

#include "transport_udp/playout_queue.h"

using namespace rva::udp;

int main() {
    PlayoutQueue queue;
    queue.Open(1);
    for (size_t index = 0; index < PlayoutQueue::kCapacity; ++index) {
        PlayoutFrame frame{.kind = PlayoutKind::kAudio, .generation = 1};
        frame.payload_size = 1;
        frame.payload[0] = static_cast<uint8_t>(index);
        assert(queue.Push(frame) == PlayoutPushResult::kAccepted);
    }
    assert(queue.size() == 4);
    queue.AdvanceGeneration(2);
    PlayoutFrame output;
    assert(queue.size() == 0);
    assert(!queue.Pop(&output));
    assert(queue.Push({.kind = PlayoutKind::kAudio, .generation = 1}) ==
           PlayoutPushResult::kStale);
    assert(queue.Push({.kind = PlayoutKind::kAudio, .generation = 2}) ==
           PlayoutPushResult::kAccepted);
    assert(queue.Pop(&output) && output.generation == 2);

    PlayoutFrame expired{
        .kind = PlayoutKind::kAudio,
        .generation = 2,
        .arrived_us = 100,
    };
    expired.payload_size = 1;
    assert(queue.Push(expired) == PlayoutPushResult::kAccepted);
    uint32_t expired_count = 0;
    assert(!queue.PopFresh(&output, 360101, 360000, &expired_count));
    assert(expired_count == 1);
    assert(queue.size() == 0);

    PlayoutFrame fresh{
        .kind = PlayoutKind::kAudio,
        .generation = 2,
        .arrived_us = 200,
    };
    fresh.payload_size = 1;
    assert(queue.Push(fresh) == PlayoutPushResult::kAccepted);
    assert(queue.PopFresh(&output, 360200, 360000, &expired_count));
    assert(expired_count == 0 && output.arrived_us == 200);

    PlayoutFrame stale_plc{
        .kind = PlayoutKind::kPlc,
        .generation = 2,
        .arrived_us = 500,
    };
    assert(queue.Push(stale_plc) == PlayoutPushResult::kAccepted);
    assert(queue.PopFresh(&output, 360500, 360000, &expired_count));
    assert(expired_count == 0 && output.kind == PlayoutKind::kPlc);
    assert(queue.Push(stale_plc) == PlayoutPushResult::kAccepted);
    assert(!queue.PopFresh(&output, 360501, 360000, &expired_count));
    assert(expired_count == 1);

    std::atomic<bool> stop{false};
    std::atomic<uint32_t> generation{2};
    std::atomic<uint32_t> last_popped{2};
    std::thread producer([&] {
        while (!stop.load()) {
            const uint32_t current = generation.load();
            queue.Push({.kind = PlayoutKind::kAudio, .generation = current});
            std::this_thread::yield();
        }
    });
    std::thread consumer([&] {
        PlayoutFrame frame;
        while (!stop.load()) {
            if (queue.Pop(&frame)) {
                const uint32_t previous = last_popped.exchange(frame.generation);
                assert(frame.generation >= previous);
            }
            std::this_thread::yield();
        }
    });
    for (uint32_t next = 3; next < 2000; ++next) {
        queue.AdvanceGeneration(next);
        generation.store(next);
    }
    queue.Close();
    stop.store(true);
    producer.join();
    consumer.join();
    assert(!queue.Pop(&output));
    assert(queue.Push({.kind = PlayoutKind::kAudio, .generation = generation.load()}) ==
           PlayoutPushResult::kClosed);
    return 0;
}
