#pragma once

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>

#include "voice_protocol/control.h"

namespace rva::runtime {

enum class PlaybackFactType : uint8_t { kStarted, kEnded };

struct PlaybackFact final {
    PlaybackFactType type = PlaybackFactType::kStarted;
    protocol::ResponseTarget target;
    protocol::PlaybackEndedOutcome outcome = protocol::PlaybackEndedOutcome::kCompleted;
    uint64_t played_samples = 0;
    uint32_t first_media_sequence = 0;
    std::optional<uint32_t> last_media_sequence;
};

// Single-owner response playout state. The playback task is the only caller;
// commands and emitted facts cross task boundaries through bounded POD queues.
class PlaybackState final {
public:
    static constexpr int64_t kDrainDeadlineUs = 1'500'000;

    void Reset();
    bool Begin(const protocol::ResponseTarget& target);
    bool SetFinalMediaSequence(
        const protocol::ResponseTarget& target,
        uint32_t final_media_sequence,
        int64_t now_us,
        std::optional<PlaybackFact>* fact);
    bool Stop(
        const protocol::ResponseTarget& target,
        uint32_t fence_generation,
        std::optional<PlaybackFact>* fact);
    bool Fail(std::optional<PlaybackFact>* fact);
    bool MarkDegraded();
    bool RequestFinalPlc(int64_t now_us, uint32_t* media_sequence);
    bool ExpireDrain(int64_t now_us, std::optional<PlaybackFact>* fact);
    [[nodiscard]] bool CanPlay(uint32_t generation) const;
    bool RecordWritten(
        uint32_t media_sequence,
        size_t samples,
        std::optional<PlaybackFact>* fact);
    bool FinishFrame(uint32_t media_sequence, std::optional<PlaybackFact>* fact);

    [[nodiscard]] bool active() const { return active_; }
    [[nodiscard]] uint32_t fence_generation() const { return fence_generation_; }

private:
    [[nodiscard]] bool TargetMatches(const protocol::ResponseTarget& target) const;
    PlaybackFact End(protocol::PlaybackEndedOutcome outcome);

    protocol::ResponseTarget target_{};
    uint32_t fence_generation_ = 0;
    uint32_t last_stop_fence_generation_ = 0;
    std::optional<uint32_t> final_media_sequence_;
    std::optional<uint32_t> last_sample_sequence_;
    std::optional<uint32_t> last_completed_sequence_;
    uint64_t played_samples_ = 0;
    int64_t drain_deadline_us_ = 0;
    bool active_ = false;
    bool started_ = false;
    bool stop_recorded_ = false;
    bool degraded_ = false;
    bool final_plc_requested_ = false;
};

}  // namespace rva::runtime
