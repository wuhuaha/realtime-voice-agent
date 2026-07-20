#include "voice_core/session.h"

namespace voice::core {

bool Session::BeginFreshSession(std::uint32_t connection_generation) {
    std::lock_guard<std::recursive_mutex> transition_lock(transition_mutex_);
    std::lock_guard<std::mutex> lock(mutex_);
    return gate_.BeginFreshSession(connection_generation);
}

std::optional<SessionSnapshot> Session::CommitHello(
    std::uint32_t connection_generation,
    contracts::TransportProfile profile,
    MediaOwner owner) {
    std::lock_guard<std::recursive_mutex> transition_lock(transition_mutex_);
    std::optional<SessionSnapshot> committed;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (gate_.session_generation() != connection_generation ||
            !gate_.CommitMedia(profile, owner)) {
            return std::nullopt;
        }
        snapshot_.reset();
        snapshot_.emplace(SessionSnapshot(
            connection_generation, gate_.playback_generation(), profile, owner));
        committed.emplace(*snapshot_);
    }
    events_.OnSessionEvent(SessionEvent::kCommitted, *committed);
    return IsCurrentOwner(*committed) ? committed : std::nullopt;
}

std::optional<SessionSnapshot> Session::AdvancePlaybackGeneration(
    const SessionSnapshot& owner, std::uint32_t playback_generation) {
    std::lock_guard<std::recursive_mutex> transition_lock(transition_mutex_);
    std::optional<SessionSnapshot> advanced;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!OwnerIdentityMatchesLocked(owner) ||
            !gate_.AdvancePlaybackGeneration(playback_generation)) {
            return std::nullopt;
        }
        snapshot_.reset();
        snapshot_.emplace(SessionSnapshot(
            owner.connection_generation(), playback_generation,
            owner.transport_profile(), owner.media_owner()));
        advanced.emplace(*snapshot_);
    }
    audio_.SetPlaybackGeneration(*advanced);
    if (!IsCurrentPlayback(*advanced)) {
        return std::nullopt;
    }
    events_.OnSessionEvent(
        SessionEvent::kPlaybackGenerationAdvanced, *advanced);
    return IsCurrentPlayback(*advanced) ? advanced : std::nullopt;
}

bool Session::SendControl(
    const SessionSnapshot& owner, std::string_view message) {
    std::optional<SessionSnapshot> sending;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!OwnerIdentityMatchesLocked(owner)) {
            return false;
        }
        sending.emplace(*snapshot_);
    }

    const bool sent = transport_.SendControl(*sending, message);
    std::lock_guard<std::mutex> lock(mutex_);
    return sent && OwnerIdentityMatchesLocked(*sending);
}

bool Session::OnTransportClosed(const SessionSnapshot& owner) {
    return RequestClose(owner);
}

bool Session::RequestClose(const SessionSnapshot& owner) {
    std::lock_guard<std::recursive_mutex> transition_lock(transition_mutex_);
    std::optional<SessionSnapshot> revoked;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (gate_.phase() == SessionPhase::kClosing) {
            return closing_snapshot_ &&
                   closing_snapshot_->connection_generation() ==
                       owner.connection_generation() &&
                   closing_snapshot_->transport_profile() ==
                       owner.transport_profile() &&
                   closing_snapshot_->media_owner() == owner.media_owner();
        }
        if (!OwnerIdentityMatchesLocked(owner)) {
            return false;
        }
        revoked.emplace(*snapshot_);
        if (!gate_.BeginClose()) {
            return false;
        }
        closing_snapshot_.reset();
        closing_snapshot_.emplace(*revoked);
        snapshot_.reset();
        revocation_in_progress_ = true;
    }
    NotifyRevoked(*revoked);
    {
        std::lock_guard<std::mutex> lock(mutex_);
        revocation_in_progress_ = false;
    }
    return true;
}

bool Session::RequestClose(std::uint32_t connection_generation) {
    std::lock_guard<std::recursive_mutex> transition_lock(transition_mutex_);
    std::optional<SessionSnapshot> revoked;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (gate_.session_generation() != connection_generation) {
            return false;
        }
        if (gate_.phase() == SessionPhase::kClosing) {
            return true;
        }
        if (gate_.phase() != SessionPhase::kOpening &&
            gate_.phase() != SessionPhase::kActive) {
            return false;
        }
        if (snapshot_) {
            revoked.emplace(*snapshot_);
        }
        if (!gate_.BeginClose()) {
            return false;
        }
        closing_snapshot_.reset();
        if (revoked) {
            closing_snapshot_.emplace(*revoked);
            revocation_in_progress_ = true;
        }
        snapshot_.reset();
    }
    if (revoked) {
        NotifyRevoked(*revoked);
        std::lock_guard<std::mutex> lock(mutex_);
        revocation_in_progress_ = false;
    }
    return true;
}

bool Session::FinishClose(std::uint32_t connection_generation) {
    std::lock_guard<std::recursive_mutex> transition_lock(transition_mutex_);
    std::lock_guard<std::mutex> lock(mutex_);
    if (revocation_in_progress_ ||
        gate_.session_generation() != connection_generation ||
        !gate_.FinishClose()) {
        return false;
    }
    closing_snapshot_.reset();
    return true;
}

std::optional<SessionSnapshot> Session::snapshot() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return snapshot_;
}

bool Session::IsCurrentOwner(const SessionSnapshot& snapshot) const {
    std::lock_guard<std::mutex> lock(mutex_);
    return OwnerIdentityMatchesLocked(snapshot);
}

bool Session::IsCurrentPlayback(const SessionSnapshot& snapshot) const {
    std::lock_guard<std::mutex> lock(mutex_);
    return PlaybackSnapshotMatchesLocked(snapshot);
}

SessionPhase Session::phase() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return gate_.phase();
}

bool Session::OwnerIdentityMatchesLocked(
    const SessionSnapshot& snapshot) const {
    return gate_.phase() == SessionPhase::kActive && snapshot_ &&
           snapshot_->connection_generation() ==
               snapshot.connection_generation() &&
           snapshot_->transport_profile() == snapshot.transport_profile() &&
           snapshot_->media_owner() == snapshot.media_owner();
}

bool Session::PlaybackSnapshotMatchesLocked(
    const SessionSnapshot& snapshot) const {
    return OwnerIdentityMatchesLocked(snapshot) &&
           snapshot_->playback_generation() == snapshot.playback_generation();
}

void Session::NotifyRevoked(const SessionSnapshot& snapshot) noexcept {
    transport_.Revoke(snapshot);
    audio_.Revoke(snapshot);
    events_.OnSessionEvent(SessionEvent::kRevoked, snapshot);
}

}  // namespace voice::core
