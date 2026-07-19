#include "voice_contracts/transport_profile.h"
#include "voice_core/session_gate.h"

#include <iostream>

namespace {

int Expect(bool condition, int error_code) {
    return condition ? 0 : error_code;
}

int TestProfiles() {
    using voice::contracts::ParseTransportProfile;
    using voice::contracts::ToWireName;
    using voice::contracts::TransportProfile;

    if (Expect(ParseTransportProfile("wss-opus-v1") == TransportProfile::kWssOpusV1, 10)) return 10;
    if (Expect(ParseTransportProfile("udp-opus-gcm-v1") == TransportProfile::kUdpOpusGcmV1, 11)) return 11;
    if (Expect(!ParseTransportProfile("websocket"), 12)) return 12;
    if (Expect(ToWireName(TransportProfile::kWssOpusV1) == "wss-opus-v1", 13)) return 13;
    if (Expect(ToWireName(TransportProfile::kUdpOpusGcmV1) == "udp-opus-gcm-v1", 14)) return 14;
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
    if (Expect(!gate.CommitMedia(TransportProfile::kUdpOpusGcmV1, MediaOwner::kWss), 24)) return 24;
    if (Expect(gate.CommitMedia(TransportProfile::kUdpOpusGcmV1, MediaOwner::kUdp), 25)) return 25;
    if (Expect(gate.media_owner() == MediaOwner::kUdp, 26)) return 26;
    if (Expect(!gate.CommitMedia(TransportProfile::kWssOpusV1, MediaOwner::kWss), 27)) return 27;
    if (Expect(!gate.AdvancePlaybackGeneration(1), 28)) return 28;
    if (Expect(gate.AdvancePlaybackGeneration(2), 29)) return 29;
    if (Expect(gate.BeginClose(), 30)) return 30;
    if (Expect(gate.media_owner() == MediaOwner::kNone, 31)) return 31;
    if (Expect(!gate.AdvancePlaybackGeneration(3), 32)) return 32;
    if (Expect(gate.FinishClose(), 33)) return 33;
    if (Expect(!gate.BeginFreshSession(1), 34)) return 34;
    if (Expect(gate.BeginFreshSession(2), 35)) return 35;
    if (Expect(gate.BeginClose() && gate.FinishClose(), 36)) return 36;
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
    std::cout << "Headless firmware contracts passed.\n";
    return 0;
}
