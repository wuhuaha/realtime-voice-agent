#pragma once

#include <array>
#include <cstddef>
#include <cstdint>

#include "transport_udp/types.h"

namespace rva::udp {

enum class PlayoutKind : uint8_t { kNone, kAudio, kPlc };
enum class JitterInsertResult : uint8_t { kAccepted, kLate, kDuplicate, kOutOfWindow, kFull };

struct PlayoutFrame final {
    PlayoutKind kind = PlayoutKind::kNone;
    uint32_t timestamp = 0;
    uint32_t generation = 0;
    int64_t arrived_us = 0;
    uint16_t payload_size = 0;
    std::array<uint8_t, wire::kMaxPayloadBytes> payload{};
};

class JitterBuffer final {
public:
    static constexpr size_t kSlotCount = 4;
    static constexpr int64_t kWaitUs = 120000;
    JitterInsertResult InsertAudio(uint32_t sequence, uint32_t timestamp,
                                   uint32_t generation, const uint8_t* payload,
                                   size_t payload_size, int64_t arrived_us, Stats* stats);
    JitterInsertResult InsertControl(uint32_t sequence, uint32_t generation,
                                     int64_t arrived_us, Stats* stats);
    PlayoutFrame Pop(int64_t now_us, Stats* stats);
    void Reset(uint32_t generation);
    void BeginAt(uint32_t sequence);
private:
    enum class SlotKind : uint8_t { kAudio, kControl };
    struct Slot final {
        bool used = false;
        SlotKind kind = SlotKind::kAudio;
        uint32_t sequence = 0;
        uint32_t timestamp = 0;
        uint32_t generation = 0;
        uint16_t payload_size = 0;
        int64_t arrived_us = 0;
        std::array<uint8_t, wire::kMaxPayloadBytes> payload{};
    };
    JitterInsertResult Reserve(uint32_t sequence, uint32_t generation,
                               int64_t arrived_us, Slot** slot, Stats* stats);
    static void ClearSlot(Slot* slot);
    std::array<Slot, kSlotCount> slots_{};
    uint32_t generation_ = 1;
    uint32_t expected_ = 0;
    bool cursor_initialized_ = false;
};

}  // namespace rva::udp
