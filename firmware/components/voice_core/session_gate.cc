#include "voice_core/session_gate.h"

namespace voice::core {

namespace {

bool OwnerMatchesProfile(contracts::TransportProfile profile, MediaOwner owner) {
    switch (profile) {
        case contracts::TransportProfile::kWssOpusV1:
            return owner == MediaOwner::kWss;
        case contracts::TransportProfile::kUdpOpusGcmV1:
            return owner == MediaOwner::kUdp;
    }
    return false;
}

}  // namespace

bool SessionGate::BeginFreshSession(std::uint32_t session_generation) {
    if (phase_ != SessionPhase::kIdle || session_generation == 0 ||
        session_generation <= last_session_generation_) {
        return false;
    }
    phase_ = SessionPhase::kOpening;
    session_generation_ = session_generation;
    playback_generation_ = 0;
    media_owner_ = MediaOwner::kNone;
    transport_profile_.reset();
    return true;
}

bool SessionGate::CommitMedia(contracts::TransportProfile profile, MediaOwner owner) {
    if (phase_ != SessionPhase::kOpening || !OwnerMatchesProfile(profile, owner)) {
        return false;
    }
    transport_profile_ = profile;
    media_owner_ = owner;
    phase_ = SessionPhase::kActive;
    return true;
}

bool SessionGate::AdvancePlaybackGeneration(std::uint32_t generation) {
    if (phase_ != SessionPhase::kActive || generation <= playback_generation_) {
        return false;
    }
    playback_generation_ = generation;
    return true;
}

bool SessionGate::BeginClose() {
    if (phase_ != SessionPhase::kOpening && phase_ != SessionPhase::kActive) {
        return false;
    }
    phase_ = SessionPhase::kClosing;
    media_owner_ = MediaOwner::kNone;
    transport_profile_.reset();
    return true;
}

bool SessionGate::FinishClose() {
    if (phase_ != SessionPhase::kClosing) {
        return false;
    }
    last_session_generation_ = session_generation_;
    session_generation_ = 0;
    playback_generation_ = 0;
    phase_ = SessionPhase::kIdle;
    return true;
}

}  // namespace voice::core
