#include "transport_udp/jitter_buffer.h"

#include <algorithm>
#include <cstring>

namespace rva::udp {

JitterInsertResult JitterBuffer::Reserve(uint32_t sequence, uint32_t generation,
                                         int64_t arrived_us, Slot** output,
                                         Stats* stats) {
    if (output == nullptr || generation != generation_) {
        return JitterInsertResult::kOutOfWindow;
    }
    if (!cursor_initialized_) {
        expected_ = sequence;
        cursor_initialized_ = true;
    }
    if (sequence < expected_) {
        if (stats) stats->late++;
        return JitterInsertResult::kLate;
    }
    if (sequence - expected_ >= kSlotCount) {
        if (stats) stats->queue_dropped++;
        return JitterInsertResult::kOutOfWindow;
    }
    Slot* free = nullptr;
    for (auto& slot : slots_) {
        if (slot.used && slot.sequence == sequence) return JitterInsertResult::kDuplicate;
        if (!slot.used && free == nullptr) free = &slot;
    }
    if (free == nullptr) {
        if (stats) stats->queue_dropped++;
        return JitterInsertResult::kFull;
    }
    free->used = true;
    free->sequence = sequence;
    free->generation = generation;
    free->arrived_us = arrived_us;
    *output = free;
    return JitterInsertResult::kAccepted;
}

JitterInsertResult JitterBuffer::InsertAudio(
        uint32_t sequence, uint32_t timestamp, uint32_t generation,
        const uint8_t* payload, size_t payload_size, int64_t arrived_us, Stats* stats) {
    if (payload == nullptr || payload_size == 0 || payload_size > wire::kMaxPayloadBytes) {
        return JitterInsertResult::kOutOfWindow;
    }
    Slot* slot = nullptr;
    const auto result = Reserve(sequence, generation, arrived_us, &slot, stats);
    if (result != JitterInsertResult::kAccepted) return result;
    slot->kind = SlotKind::kAudio;
    slot->timestamp = timestamp;
    slot->payload_size = static_cast<uint16_t>(payload_size);
    std::memcpy(slot->payload.data(), payload, payload_size);
    return JitterInsertResult::kAccepted;
}

JitterInsertResult JitterBuffer::InsertControl(uint32_t sequence, uint32_t generation,
                                               int64_t arrived_us, Stats* stats) {
    Slot* slot = nullptr;
    const auto result = Reserve(sequence, generation, arrived_us, &slot, stats);
    if (result != JitterInsertResult::kAccepted) return result;
    slot->kind = SlotKind::kControl;
    slot->timestamp = 0;
    slot->payload_size = 0;
    return JitterInsertResult::kAccepted;
}

PlayoutFrame JitterBuffer::Pop(int64_t now_us, Stats* stats) {
    if (!cursor_initialized_) return {};
    for (;;) {
        Slot* expected = nullptr;
        for (auto& slot : slots_) {
            if (slot.used && slot.sequence == expected_) {
                expected = &slot;
                break;
            }
        }
        if (expected == nullptr) break;
        if (expected->kind == SlotKind::kControl) {
            ClearSlot(expected);
            expected_++;
            continue;
        }
        PlayoutFrame output{.kind = PlayoutKind::kAudio,
                            .timestamp = expected->timestamp,
                            .generation = expected->generation,
                            .payload_size = expected->payload_size};
        std::copy_n(expected->payload.begin(), expected->payload_size, output.payload.begin());
        const int64_t age = std::max<int64_t>(0, now_us - expected->arrived_us);
        if (stats) {
            stats->played++;
            stats->max_media_age_ms = std::max(
                stats->max_media_age_ms, static_cast<uint32_t>(age / 1000));
        }
        ClearSlot(expected);
        expected_++;
        return output;
    }

    Slot* first = nullptr;
    for (auto& slot : slots_) {
        if (slot.used && slot.sequence > expected_ &&
            (first == nullptr || slot.sequence < first->sequence)) {
            first = &slot;
        }
    }
    if (first == nullptr || now_us - first->arrived_us < kWaitUs) return {};
    const uint32_t missing = first->sequence - expected_;
    constexpr uint32_t kSamplesPerFrame = 960;
    const uint32_t timestamp = first->timestamp >= missing * kSamplesPerFrame
                                   ? first->timestamp - missing * kSamplesPerFrame
                                   : 0;
    expected_++;
    if (stats) stats->lost++;
    return {.kind = PlayoutKind::kPlc, .timestamp = timestamp, .generation = generation_};
}

void JitterBuffer::Reset(uint32_t generation) {
    for (auto& slot : slots_) ClearSlot(&slot);
    generation_ = generation;
    cursor_initialized_ = false;
    expected_ = 0;
}

void JitterBuffer::BeginAt(uint32_t sequence) {
    expected_ = sequence;
    cursor_initialized_ = true;
}

void JitterBuffer::ClearSlot(Slot* slot) {
    volatile uint8_t* bytes = slot->payload.data();
    for (size_t index = 0; index < slot->payload.size(); ++index) bytes[index] = 0;
    *slot = {};
}

}  // namespace rva::udp
