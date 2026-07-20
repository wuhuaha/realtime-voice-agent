#pragma once

#include "voice_core/session_gate.h"

#include <cstdint>
#include <mutex>
#include <optional>
#include <string_view>

namespace voice::core {

class SessionSnapshot final {
public:
    SessionSnapshot(const SessionSnapshot&) = default;
    SessionSnapshot& operator=(const SessionSnapshot&) = delete;

    std::uint32_t connection_generation() const {
        return connection_generation_;
    }
    std::uint32_t playback_generation() const { return playback_generation_; }
    contracts::TransportProfile transport_profile() const {
        return transport_profile_;
    }
    MediaOwner media_owner() const { return media_owner_; }

private:
    friend class Session;

    SessionSnapshot(
        std::uint32_t connection_generation,
        std::uint32_t playback_generation,
        contracts::TransportProfile transport_profile,
        MediaOwner media_owner)
        : connection_generation_(connection_generation),
          playback_generation_(playback_generation),
          transport_profile_(transport_profile),
          media_owner_(media_owner) {}

    const std::uint32_t connection_generation_;
    const std::uint32_t playback_generation_;
    const contracts::TransportProfile transport_profile_;
    const MediaOwner media_owner_;
};

enum class SessionEvent {
    kCommitted,
    kPlaybackGenerationAdvanced,
    kRevoked,
};

class TransportPort {
public:
    virtual ~TransportPort() = default;
    virtual bool SendControl(
        const SessionSnapshot& snapshot, std::string_view message) noexcept = 0;
    virtual void Revoke(const SessionSnapshot& snapshot) noexcept = 0;
};

class AudioPort {
public:
    virtual ~AudioPort() = default;
    virtual void SetPlaybackGeneration(const SessionSnapshot& snapshot) noexcept = 0;
    virtual void Revoke(const SessionSnapshot& snapshot) noexcept = 0;
};

class EventSink {
public:
    virtual ~EventSink() = default;
    virtual void OnSessionEvent(
        SessionEvent event, const SessionSnapshot& snapshot) noexcept = 0;
};

class Session final {
public:
    Session(TransportPort& transport, AudioPort& audio, EventSink& events)
        : transport_(transport), audio_(audio), events_(events) {}

    bool BeginFreshSession(std::uint32_t connection_generation);
    std::optional<SessionSnapshot> CommitHello(
        std::uint32_t connection_generation,
        contracts::TransportProfile profile,
        MediaOwner owner);
    std::optional<SessionSnapshot> AdvancePlaybackGeneration(
        const SessionSnapshot& owner, std::uint32_t playback_generation);

    bool SendControl(
        const SessionSnapshot& owner, std::string_view message);
    bool OnTransportClosed(const SessionSnapshot& owner);
    bool RequestClose(const SessionSnapshot& owner);
    bool RequestClose(std::uint32_t connection_generation);
    bool FinishClose(std::uint32_t connection_generation);

    std::optional<SessionSnapshot> snapshot() const;
    bool IsCurrentOwner(const SessionSnapshot& snapshot) const;
    bool IsCurrentPlayback(const SessionSnapshot& snapshot) const;
    SessionPhase phase() const;

private:
    bool OwnerIdentityMatchesLocked(const SessionSnapshot& snapshot) const;
    bool PlaybackSnapshotMatchesLocked(const SessionSnapshot& snapshot) const;
    void NotifyRevoked(const SessionSnapshot& snapshot) noexcept;

    TransportPort& transport_;
    AudioPort& audio_;
    EventSink& events_;
    // Serializes state transitions with their externally visible effects while
    // allowing a port callback to synchronously request close.
    mutable std::recursive_mutex transition_mutex_;
    mutable std::mutex mutex_;
    SessionGate gate_;
    std::optional<SessionSnapshot> snapshot_;
    std::optional<SessionSnapshot> closing_snapshot_;
    bool revocation_in_progress_ = false;
};

}  // namespace voice::core
