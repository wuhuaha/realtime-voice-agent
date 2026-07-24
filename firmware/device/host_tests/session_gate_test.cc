#include "voice_contracts/transport_profile.h"
#include "voice_core/session.h"
#include "voice_core/session_gate.h"

#include <chrono>
#include <condition_variable>
#include <future>
#include <iostream>
#include <mutex>
#include <string_view>
#include <type_traits>
#include <vector>

namespace {

int Expect(bool condition, int error_code) {
    return condition ? 0 : error_code;
}

int TestProfiles() {
    using voice::contracts::ParseTransportProfile;
    using voice::contracts::ToWireName;
    using voice::contracts::TransportProfile;

    if (Expect(ParseTransportProfile("wss-opus-v3") == TransportProfile::kWssOpusV3, 10)) return 10;
    if (Expect(ParseTransportProfile("udp-opus-gcm-v2") == TransportProfile::kUdpOpusGcmV2, 11)) return 11;
    if (Expect(!ParseTransportProfile("wss-opus-v2"), 15)) return 15;
    if (Expect(!ParseTransportProfile("websocket"), 12)) return 12;
    if (Expect(ToWireName(TransportProfile::kWssOpusV3) == "wss-opus-v3", 13)) return 13;
    if (Expect(ToWireName(TransportProfile::kUdpOpusGcmV2) == "udp-opus-gcm-v2", 14)) return 14;
    return 0;
}

int TestSessionGate() {
    using voice::contracts::TransportProfile;
    using voice::core::MediaOwner;
    using voice::core::SessionGate;
    using voice::core::SessionPhase;

    SessionGate gate;
    if (Expect(gate.phase() == SessionPhase::kIdle, 20)) return 20;
    if (Expect(!gate.BeginFreshSession(0), 21)) return 21;
    if (Expect(gate.BeginFreshSession(1), 22)) return 22;
    if (Expect(!gate.BeginFreshSession(2), 23)) return 23;
    if (Expect(!gate.CommitMedia(TransportProfile::kUdpOpusGcmV2, MediaOwner::kWss), 24)) return 24;
    if (Expect(gate.CommitMedia(TransportProfile::kUdpOpusGcmV2, MediaOwner::kUdp), 25)) return 25;
    if (Expect(gate.media_owner() == MediaOwner::kUdp, 26)) return 26;
    if (Expect(!gate.CommitMedia(TransportProfile::kWssOpusV3, MediaOwner::kWss), 27)) return 27;
    if (Expect(gate.AdvancePlaybackGeneration(1), 28)) return 28;
    if (Expect(!gate.AdvancePlaybackGeneration(1), 29)) return 29;
    if (Expect(gate.BeginClose(), 30)) return 30;
    if (Expect(gate.media_owner() == MediaOwner::kNone, 31)) return 31;
    if (Expect(!gate.AdvancePlaybackGeneration(3), 32)) return 32;
    if (Expect(gate.FinishClose(), 33)) return 33;
    if (Expect(!gate.BeginFreshSession(1), 34)) return 34;
    if (Expect(gate.BeginFreshSession(2), 35)) return 35;
    if (Expect(gate.BeginClose() && gate.FinishClose(), 36)) return 36;

    if (Expect(gate.BeginFreshSession(3), 37)) return 37;
    if (Expect(gate.CommitMedia(TransportProfile::kWssOpusV3, MediaOwner::kWss), 38)) return 38;
    if (Expect(gate.BeginClose() && gate.FinishClose(), 39)) return 39;
    return 0;
}

class FakeTransport final : public voice::core::TransportPort {
public:
    bool SendControl(
        const voice::core::SessionSnapshot& snapshot,
        std::string_view message) noexcept override {
        ++send_calls;
        last_send_generation = snapshot.connection_generation();
        if (message.empty()) return false;
        if (close_during_send) {
            session->RequestClose(snapshot);
        }
        return true;
    }

    void Revoke(const voice::core::SessionSnapshot& snapshot) noexcept override {
        ++revoke_calls;
        last_revoked_generation = snapshot.connection_generation();
    }

    voice::core::Session* session = nullptr;
    bool close_during_send = false;
    int send_calls = 0;
    int revoke_calls = 0;
    std::uint32_t last_send_generation = 0;
    std::uint32_t last_revoked_generation = 0;
};

class FakeAudio final : public voice::core::AudioPort {
public:
    void SetPlaybackGeneration(
        const voice::core::SessionSnapshot& snapshot) noexcept override {
        ++generation_calls;
        last_playback_generation = snapshot.playback_generation();
        if (close_during_generation) {
            close_during_generation_result = session->RequestClose(snapshot);
        }
    }

    void Revoke(const voice::core::SessionSnapshot& snapshot) noexcept override {
        ++revoke_calls;
        last_revoked_generation = snapshot.connection_generation();
        if (finish_during_revoke) {
            finish_during_revoke_result =
                session->FinishClose(snapshot.connection_generation());
        }
    }

    voice::core::Session* session = nullptr;
    bool close_during_generation = false;
    bool close_during_generation_result = false;
    bool finish_during_revoke = false;
    bool finish_during_revoke_result = true;
    int generation_calls = 0;
    int revoke_calls = 0;
    std::uint32_t last_playback_generation = 0;
    std::uint32_t last_revoked_generation = 0;
};

class FakeEvents final : public voice::core::EventSink {
public:
    void OnSessionEvent(
        voice::core::SessionEvent event,
        const voice::core::SessionSnapshot& snapshot) noexcept override {
        events.push_back(event);
        generations.push_back(snapshot.connection_generation());
        if (close_on_commit && event == voice::core::SessionEvent::kCommitted) {
            close_on_commit_result = session->RequestClose(snapshot);
        }
    }

    voice::core::Session* session = nullptr;
    bool close_on_commit = false;
    bool close_on_commit_result = false;
    std::vector<voice::core::SessionEvent> events;
    std::vector<std::uint32_t> generations;
};

class BlockingTransport final : public voice::core::TransportPort {
public:
    bool SendControl(
        const voice::core::SessionSnapshot&, std::string_view) noexcept override {
        return true;
    }

    void Revoke(const voice::core::SessionSnapshot&) noexcept override {
        std::unique_lock<std::mutex> lock(mutex_);
        revoke_entered_ = true;
        condition_.notify_all();
        condition_.wait(lock, [this] { return release_revoke_; });
    }

    void WaitForRevoke() {
        std::unique_lock<std::mutex> lock(mutex_);
        condition_.wait(lock, [this] { return revoke_entered_; });
    }

    void ReleaseRevoke() {
        std::lock_guard<std::mutex> lock(mutex_);
        release_revoke_ = true;
        condition_.notify_all();
    }

private:
    std::mutex mutex_;
    std::condition_variable condition_;
    bool revoke_entered_ = false;
    bool release_revoke_ = false;
};

class BlockingSendTransport final : public voice::core::TransportPort {
public:
    bool SendControl(
        const voice::core::SessionSnapshot&, std::string_view) noexcept override {
        std::unique_lock<std::mutex> lock(mutex_);
        send_entered_ = true;
        condition_.notify_all();
        condition_.wait(lock, [this] { return revoked_; });
        return true;
    }

    void Revoke(const voice::core::SessionSnapshot&) noexcept override {
        std::lock_guard<std::mutex> lock(mutex_);
        revoked_ = true;
        condition_.notify_all();
    }

    void WaitForSend() {
        std::unique_lock<std::mutex> lock(mutex_);
        condition_.wait(lock, [this] { return send_entered_; });
    }

private:
    std::mutex mutex_;
    std::condition_variable condition_;
    bool send_entered_ = false;
    bool revoked_ = false;
};

int TestSessionBoundary() {
    using voice::contracts::TransportProfile;
    using voice::core::MediaOwner;
    using voice::core::Session;
    using voice::core::SessionEvent;
    using voice::core::SessionPhase;
    using voice::core::SessionSnapshot;

    static_assert(std::is_copy_constructible_v<SessionSnapshot>);
    static_assert(!std::is_default_constructible_v<SessionSnapshot>);
    static_assert(!std::is_copy_assignable_v<SessionSnapshot>);

    FakeTransport transport;
    FakeAudio audio;
    FakeEvents events;
    Session session(transport, audio, events);
    transport.session = &session;
    audio.session = &session;
    events.session = &session;

    if (Expect(session.BeginFreshSession(1), 40)) return 40;
    if (Expect(!session.CommitHello(
            1, TransportProfile::kUdpOpusGcmV2, MediaOwner::kWss), 41)) {
        return 41;
    }
    const auto first = session.CommitHello(
        1, TransportProfile::kUdpOpusGcmV2, MediaOwner::kUdp);
    if (Expect(first.has_value(), 42)) return 42;
    if (Expect(!session.CommitHello(
            1, TransportProfile::kUdpOpusGcmV2, MediaOwner::kUdp), 43)) {
        return 43;
    }
    if (Expect(session.IsCurrentOwner(*first), 44)) return 44;
    if (Expect(session.IsCurrentPlayback(*first), 45)) return 45;

    const auto advanced = session.AdvancePlaybackGeneration(*first, 1);
    if (Expect(advanced.has_value(), 46)) return 46;
    if (Expect(session.IsCurrentOwner(*first), 47)) return 47;
    if (Expect(!session.IsCurrentPlayback(*first), 48)) return 48;
    if (Expect(session.IsCurrentPlayback(*advanced), 49)) return 49;
    if (Expect(audio.generation_calls == 1 &&
               audio.last_playback_generation == 1, 50)) return 50;

    transport.close_during_send = true;
    if (Expect(!session.SendControl(*first, "{}"), 51)) return 51;
    if (Expect(session.phase() == SessionPhase::kClosing, 52)) return 52;
    if (Expect(!session.snapshot(), 53)) return 53;
    if (Expect(transport.revoke_calls == 1 && audio.revoke_calls == 1, 54)) {
        return 54;
    }
    if (Expect(session.RequestClose(*first), 55)) return 55;
    if (Expect(transport.revoke_calls == 1 && audio.revoke_calls == 1, 56)) {
        return 56;
    }
    if (Expect(session.FinishClose(1), 57)) return 57;

    if (Expect(!session.BeginFreshSession(1), 58)) return 58;
    if (Expect(session.BeginFreshSession(2), 59)) return 59;
    const auto second = session.CommitHello(
        2, TransportProfile::kWssOpusV3, MediaOwner::kWss);
    if (Expect(second.has_value(), 60)) return 60;
    if (Expect(!session.OnTransportClosed(*first), 61)) return 61;
    if (Expect(session.IsCurrentOwner(*second), 62)) return 62;
    if (Expect(!session.SendControl(*first, "stale"), 63)) return 63;
    if (Expect(session.RequestClose(*second) && session.FinishClose(2), 64)) {
        return 64;
    }
    if (Expect(events.events == std::vector<SessionEvent>{
            SessionEvent::kCommitted,
            SessionEvent::kPlaybackGenerationAdvanced,
            SessionEvent::kRevoked,
            SessionEvent::kCommitted,
            SessionEvent::kRevoked}, 65)) return 65;
    if (Expect(events.generations == std::vector<std::uint32_t>{1, 1, 1, 2, 2},
               66)) return 66;

    if (Expect(session.BeginFreshSession(3), 67)) return 67;
    if (Expect(session.RequestClose(3) && session.FinishClose(3), 68)) return 68;
    return 0;
}

int TestReentrantTransitionOrdering() {
    using voice::contracts::TransportProfile;
    using voice::core::MediaOwner;
    using voice::core::Session;
    using voice::core::SessionEvent;
    using voice::core::SessionPhase;

    {
        FakeTransport transport;
        FakeAudio audio;
        FakeEvents events;
        Session session(transport, audio, events);
        transport.session = &session;
        audio.session = &session;
        events.session = &session;
        events.close_on_commit = true;

        if (Expect(session.BeginFreshSession(10), 70)) return 70;
        if (Expect(!session.CommitHello(
                10, TransportProfile::kWssOpusV3, MediaOwner::kWss), 71)) {
            return 71;
        }
        if (Expect(events.close_on_commit_result, 72)) return 72;
        if (Expect(events.events == std::vector<SessionEvent>{
                SessionEvent::kCommitted, SessionEvent::kRevoked}, 73)) {
            return 73;
        }
        if (Expect(session.phase() == SessionPhase::kClosing &&
                   session.FinishClose(10), 74)) return 74;
    }

    {
        FakeTransport transport;
        FakeAudio audio;
        FakeEvents events;
        Session session(transport, audio, events);
        transport.session = &session;
        audio.session = &session;
        events.session = &session;

        if (Expect(session.BeginFreshSession(20), 75)) return 75;
        const auto owner = session.CommitHello(
            20, TransportProfile::kUdpOpusGcmV2, MediaOwner::kUdp);
        if (Expect(owner.has_value(), 76)) return 76;
        audio.close_during_generation = true;
        audio.finish_during_revoke = true;
        if (Expect(!session.AdvancePlaybackGeneration(*owner, 2), 77)) {
            return 77;
        }
        if (Expect(audio.close_during_generation_result, 78)) return 78;
        if (Expect(!audio.finish_during_revoke_result, 79)) return 79;
        if (Expect(events.events == std::vector<SessionEvent>{
                SessionEvent::kCommitted, SessionEvent::kRevoked}, 80)) {
            return 80;
        }
        if (Expect(session.FinishClose(20), 81)) return 81;
    }
    return 0;
}

int TestConcurrentCloseBarrier() {
    using namespace std::chrono_literals;
    using voice::contracts::TransportProfile;
    using voice::core::MediaOwner;
    using voice::core::Session;

    BlockingTransport transport;
    FakeAudio audio;
    FakeEvents events;
    Session session(transport, audio, events);
    audio.session = &session;
    events.session = &session;

    if (Expect(session.BeginFreshSession(30), 82)) return 82;
    const auto owner = session.CommitHello(
        30, TransportProfile::kWssOpusV3, MediaOwner::kWss);
    if (Expect(owner.has_value(), 83)) return 83;

    auto close = std::async(std::launch::async, [&] {
        return session.RequestClose(*owner);
    });
    transport.WaitForRevoke();
    auto finish = std::async(std::launch::async, [&] {
        return session.FinishClose(30);
    });
    if (Expect(finish.wait_for(50ms) == std::future_status::timeout, 84)) {
        transport.ReleaseRevoke();
        close.wait();
        return 84;
    }
    transport.ReleaseRevoke();
    if (Expect(close.get(), 85)) return 85;
    if (Expect(finish.get(), 86)) return 86;
    return 0;
}

int TestCloseRevokesBlockingSend() {
    using voice::contracts::TransportProfile;
    using voice::core::MediaOwner;
    using voice::core::Session;

    BlockingSendTransport transport;
    FakeAudio audio;
    FakeEvents events;
    Session session(transport, audio, events);
    audio.session = &session;
    events.session = &session;

    if (Expect(session.BeginFreshSession(40), 87)) return 87;
    const auto owner = session.CommitHello(
        40, TransportProfile::kWssOpusV3, MediaOwner::kWss);
    if (Expect(owner.has_value(), 88)) return 88;

    auto send = std::async(std::launch::async, [&] {
        return session.SendControl(*owner, "blocking");
    });
    transport.WaitForSend();
    auto close = std::async(std::launch::async, [&] {
        return session.RequestClose(*owner);
    });
    if (Expect(close.get(), 89)) return 89;
    if (Expect(!send.get(), 90)) return 90;
    if (Expect(session.FinishClose(40), 91)) return 91;
    return 0;
}

}  // namespace

int main() {
    if (const int result = TestProfiles(); result != 0) {
        return result;
    }
    if (const int result = TestSessionGate(); result != 0) {
        return result;
    }
    if (const int result = TestSessionBoundary(); result != 0) {
        return result;
    }
    if (const int result = TestReentrantTransitionOrdering(); result != 0) {
        return result;
    }
    if (const int result = TestConcurrentCloseBarrier(); result != 0) {
        return result;
    }
    if (const int result = TestCloseRevokesBlockingSend(); result != 0) {
        return result;
    }
    std::cout << "Headless firmware contracts passed.\n";
    return 0;
}
