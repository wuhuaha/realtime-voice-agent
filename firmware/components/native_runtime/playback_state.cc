#include "native_runtime/playback_state.h"

#include <limits>

namespace rva::runtime {

void PlaybackState::Reset() {
    target_ = {};
    fence_generation_ = 0;
    last_stop_fence_generation_ = 0;
    final_media_sequence_.reset();
    last_sample_sequence_.reset();
    last_completed_sequence_.reset();
    played_samples_ = 0;
    active_ = false;
    started_ = false;
    stop_recorded_ = false;
}

bool PlaybackState::Begin(const protocol::ResponseTarget& target) {
    if (active_ || target.response_id.empty() || target.generation == 0 ||
        target.generation <= fence_generation_) {
        return false;
    }
    target_ = target;
    final_media_sequence_.reset();
    last_sample_sequence_.reset();
    last_completed_sequence_.reset();
    played_samples_ = 0;
    active_ = true;
    started_ = false;
    last_stop_fence_generation_ = 0;
    stop_recorded_ = false;
    return true;
}

bool PlaybackState::SetFinalMediaSequence(
    const protocol::ResponseTarget& target,
    uint32_t final_media_sequence,
    std::optional<PlaybackFact>* fact) {
    if (fact == nullptr || !active_ || !TargetMatches(target) ||
        final_media_sequence_.has_value() ||
        (last_completed_sequence_.has_value() &&
         *last_completed_sequence_ > final_media_sequence)) {
        return false;
    }
    fact->reset();
    final_media_sequence_ = final_media_sequence;
    if (last_completed_sequence_ == final_media_sequence_) {
        *fact = End(protocol::PlaybackEndedOutcome::kCompleted);
    }
    return true;
}

bool PlaybackState::Stop(
    const protocol::ResponseTarget& target,
    uint32_t fence_generation,
    std::optional<PlaybackFact>* fact) {
    if (fact == nullptr) return false;
    fact->reset();
    if (stop_recorded_ && TargetMatches(target)) {
        return fence_generation == last_stop_fence_generation_;
    }
    if (!active_ || !TargetMatches(target) ||
        fence_generation <= target_.generation || fence_generation <= fence_generation_) {
        return false;
    }
    last_stop_fence_generation_ = fence_generation;
    stop_recorded_ = true;
    fence_generation_ = fence_generation;
    *fact = End(protocol::PlaybackEndedOutcome::kStopped);
    return true;
}

bool PlaybackState::Fail(std::optional<PlaybackFact>* fact) {
    if (fact == nullptr || !active_) return false;
    *fact = End(protocol::PlaybackEndedOutcome::kFailed);
    return true;
}

bool PlaybackState::CanPlay(uint32_t generation) const {
    return active_ && generation == target_.generation && generation > fence_generation_;
}

bool PlaybackState::RecordWritten(
    uint32_t media_sequence,
    size_t samples,
    std::optional<PlaybackFact>* fact) {
    if (fact == nullptr || !active_ || samples == 0 ||
        samples > std::numeric_limits<uint64_t>::max() - played_samples_ ||
        (last_sample_sequence_.has_value() && media_sequence < *last_sample_sequence_)) {
        return false;
    }
    fact->reset();
    played_samples_ += samples;
    last_sample_sequence_ = media_sequence;
    if (!started_) {
        started_ = true;
        *fact = PlaybackFact{
            .type = PlaybackFactType::kStarted,
            .target = target_,
            .played_samples = played_samples_,
            .first_media_sequence = media_sequence,
            .last_media_sequence = std::nullopt,
        };
    }
    return true;
}

bool PlaybackState::FinishFrame(
    uint32_t media_sequence,
    std::optional<PlaybackFact>* fact) {
    if (fact == nullptr || !active_ || !started_ ||
        last_sample_sequence_ != media_sequence ||
        (last_completed_sequence_.has_value() &&
         media_sequence <= *last_completed_sequence_) ||
        (final_media_sequence_.has_value() &&
         media_sequence > *final_media_sequence_)) {
        return false;
    }
    fact->reset();
    last_completed_sequence_ = media_sequence;
    if (final_media_sequence_ == last_completed_sequence_) {
        *fact = End(protocol::PlaybackEndedOutcome::kCompleted);
    }
    return true;
}

bool PlaybackState::TargetMatches(const protocol::ResponseTarget& target) const {
    return target.response_id == target_.response_id &&
           target.generation == target_.generation;
}

PlaybackFact PlaybackState::End(protocol::PlaybackEndedOutcome outcome) {
    PlaybackFact fact{
        .type = PlaybackFactType::kEnded,
        .target = target_,
        .outcome = outcome,
        .played_samples = played_samples_,
        .last_media_sequence = started_ ? last_sample_sequence_ : std::nullopt,
    };
    if (outcome != protocol::PlaybackEndedOutcome::kStopped) {
        fence_generation_ = target_.generation;
        target_ = {};
    }
    final_media_sequence_.reset();
    last_sample_sequence_.reset();
    last_completed_sequence_.reset();
    played_samples_ = 0;
    active_ = false;
    started_ = false;
    return fact;
}

}  // namespace rva::runtime
