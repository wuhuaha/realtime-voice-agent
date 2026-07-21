#pragma once

#include "voice_contracts/transport_profile.h"

#include <cstdint>
#include <optional>

namespace voice::core {

enum class SessionPhase {
    kIdle,
    kOpening,
    kActive,
    kClosing,
};

enum class MediaOwner {
    kNone,
    kWss,
    kUdp,
};

class SessionGate {
public:
    bool BeginFreshSession(std::uint32_t session_generation);
    bool CommitMedia(contracts::TransportProfile profile, MediaOwner owner);
    bool AdvancePlaybackGeneration(std::uint32_t generation);
    bool BeginClose();
    bool FinishClose();

    SessionPhase phase() const { return phase_; }
    MediaOwner media_owner() const { return media_owner_; }
    std::optional<contracts::TransportProfile> transport_profile() const {
        return transport_profile_;
    }
    std::uint32_t session_generation() const { return session_generation_; }
    std::uint32_t playback_generation() const { return playback_generation_; }

private:
    SessionPhase phase_ = SessionPhase::kIdle;
    MediaOwner media_owner_ = MediaOwner::kNone;
    std::optional<contracts::TransportProfile> transport_profile_;
    std::uint32_t last_session_generation_ = 0;
    std::uint32_t session_generation_ = 0;
    std::uint32_t playback_generation_ = 0;
};

}  // namespace voice::core
