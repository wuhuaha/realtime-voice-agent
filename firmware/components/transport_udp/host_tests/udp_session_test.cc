#include <algorithm>
#include <array>
#include <cassert>
#include <cstring>

#include "transport_udp/udp_session.h"

namespace rva::udp {
class UdpSessionTestPeer final {
public:
    static bool FenceGeneration(UdpSession* session, uint32_t generation) {
        return session->FenceGeneration(generation);
    }
    static bool AdvanceGeneration(UdpSession* session, uint32_t generation) {
        return session->AdvanceGeneration(generation);
    }
    static size_t BufferedCount(const UdpSession& session) {
        size_t count = 0;
        for (const auto& frame : session.future_frames_) count += frame.used ? 1 : 0;
        return count;
    }
    static bool BuffersAreZero(const UdpSession& session) {
        for (const auto& frame : session.future_frames_) {
            const auto* bytes = reinterpret_cast<const uint8_t*>(&frame);
            for (size_t index = 0; index < sizeof(frame); ++index) {
                if (bytes[index] != 0) return false;
            }
        }
        return true;
    }
};
}  // namespace rva::udp

namespace {

using namespace rva::udp;

class FakeAead final : public AeadPort {
public:
    bool SetKey(const Aes128Key& key) override {
        key_ = key;
        keyed_ = true;
        return allow_key_;
    }
    void ClearKey() override {
        key_.fill(0);
        keyed_ = false;
        clear_count_++;
    }
    bool Encrypt(const wire::Nonce& nonce, const wire::WireHeader& aad,
                 const uint8_t* plaintext, size_t size,
                 uint8_t* ciphertext, uint8_t* tag) override {
        if (!keyed_) return false;
        for (size_t i = 0; i < size; ++i) ciphertext[i] = plaintext[i] ^ key_[i % key_.size()];
        MakeTag(nonce, aad.data(), ciphertext, size, tag);
        return true;
    }
    bool Decrypt(const wire::Nonce& nonce, const uint8_t* aad,
                 const uint8_t* ciphertext, size_t size,
                 const uint8_t* tag, uint8_t* plaintext) override {
        if (!keyed_) return false;
        std::array<uint8_t, wire::kTagBytes> expected{};
        MakeTag(nonce, aad, ciphertext, size, expected.data());
        if (!std::equal(expected.begin(), expected.end(), tag)) return false;
        for (size_t i = 0; i < size; ++i) plaintext[i] = ciphertext[i] ^ key_[i % key_.size()];
        return true;
    }
    void set_allow_key(bool allow) { allow_key_ = allow; }
    [[nodiscard]] bool keyed() const { return keyed_; }
    [[nodiscard]] uint32_t clear_count() const { return clear_count_; }
private:
    void MakeTag(const wire::Nonce& nonce, const uint8_t* aad,
                 const uint8_t* ciphertext, size_t size, uint8_t* tag) {
        std::fill(tag, tag + wire::kTagBytes, 0);
        for (size_t i = 0; i < key_.size(); ++i) tag[i] ^= key_[i];
        for (size_t i = 0; i < nonce.size(); ++i) tag[i % wire::kTagBytes] ^= nonce[i];
        for (size_t i = 0; i < wire::kHeaderBytes; ++i) tag[i % wire::kTagBytes] ^= aad[i];
        for (size_t i = 0; i < size; ++i) tag[i % wire::kTagBytes] ^= ciphertext[i];
    }
    Aes128Key key_{};
    bool allow_key_ = true;
    bool keyed_ = false;
    uint32_t clear_count_ = 0;
};

Endpoint Server() {
    Endpoint endpoint{.address = {}, .address_bytes = 4, .port = 9000};
    endpoint.address[0] = 192; endpoint.address[1] = 0; endpoint.address[2] = 2; endpoint.address[3] = 1;
    return endpoint;
}

SessionGrant Grant() {
    SessionGrant grant{.server = Server(), .media_epoch = 0x10203040, .initial_generation = 1};
    grant.media_id = {1, 2, 3, 4, 5, 6, 7, 8};
    for (size_t i = 0; i < kAes128KeyBytes; ++i) {
        grant.uplink_key[i] = static_cast<uint8_t>(i);
        grant.downlink_key[i] = static_cast<uint8_t>(0xF0 + i);
    }
    grant.uplink_salt = {0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0xA6, 0xA7};
    grant.downlink_salt = {0xB0, 0xB1, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6, 0xB7};
    return grant;
}

size_t Downlink(FakeAead& crypto, const SessionGrant& grant, wire::DatagramType type,
                uint32_t sequence, uint32_t generation, const uint8_t* payload,
                size_t payload_size, std::array<uint8_t, wire::kMaxDatagramBytes>& output) {
    wire::Header header{.type = type, .media_id = grant.media_id,
                        .media_epoch = grant.media_epoch, .sequence = sequence,
                        .timestamp = type == wire::DatagramType::kAudio ? sequence * 960 : 0,
                        .generation = generation,
                        .payload_length = static_cast<uint32_t>(payload_size)};
    const auto aad = wire::EncodeHeader(header);
    std::copy(aad.begin(), aad.end(), output.begin());
    crypto.Encrypt(wire::MakeNonce(grant.downlink_salt, sequence), aad, payload,
                   payload_size, output.data() + wire::kHeaderBytes,
                   output.data() + wire::kHeaderBytes + payload_size);
    return wire::kHeaderBytes + payload_size + wire::kTagBytes;
}

}  // namespace

int main() {
    Endpoint malformed = Server();
    malformed.address_bytes = 255;
    assert(!(malformed == Server()));

    ReplayWindow replay;
    assert(replay.CanAccept(0));
    replay.Commit(0);
    assert(!replay.CanAccept(0));
    assert(replay.CanAccept(1024));
    replay.Commit(1024);
    assert(!replay.CanAccept(2049));
    assert(replay.CanAccept(1023));
    replay.Commit(1023);
    assert(!replay.CanAccept(1023));

    FakeAead uplink;
    FakeAead downlink;
    UdpSession session(uplink, downlink);
    const SessionGrant grant = Grant();
    assert(session.Configure(grant));

    std::array<uint8_t, wire::kMaxDatagramBytes> uplink_datagram{};
    size_t uplink_size = 0;
    assert(session.BuildProbe(uplink_datagram.data(), uplink_datagram.size(), &uplink_size));
    const auto probe = wire::ParseDatagram(
        uplink_datagram.data(), uplink_size, wire::Direction::kUplink);
    assert(probe && probe->header.type == wire::DatagramType::kProbe);
    assert(probe->header.sequence == 0 && probe->header.generation == 0);

    assert(session.BuildKeepalive(
        uplink_datagram.data(), uplink_datagram.size(), &uplink_size));
    const auto keepalive = wire::ParseDatagram(
        uplink_datagram.data(), uplink_size, wire::Direction::kUplink);
    assert(keepalive && keepalive->header.type == wire::DatagramType::kKeepalive);
    assert(keepalive->header.sequence == 1 && keepalive->header.generation == 1);

    std::array<uint8_t, wire::kMaxDatagramBytes> datagram{};
    size_t size = Downlink(downlink, grant, wire::DatagramType::kProbeAck,
                           0, 1, nullptr, 0, datagram);
    assert(session.Receive(Server(), datagram.data(), size, 0) ==
           AdmissionResult::kAcceptedProbeAck);
    assert(session.Receive(Server(), datagram.data(), size, 0) == AdmissionResult::kReplay);

    // A valid tag from a different endpoint cannot establish source pinning.
    FakeAead source_uplink;
    FakeAead source_downlink;
    UdpSession source_session(source_uplink, source_downlink);
    assert(source_session.Configure(grant));
    Endpoint untrusted = Server();
    untrusted.port++;
    assert(source_session.Receive(untrusted, datagram.data(), size, 0) ==
           AdmissionResult::kWrongSource);
    assert(source_session.Receive(Server(), datagram.data(), size, 0) ==
           AdmissionResult::kAcceptedProbeAck);

    assert(UdpSessionTestPeer::AdvanceGeneration(&session, 1));
    const uint8_t opus[] = {0xF8, 0xFF, 0xFE};
    size = Downlink(downlink, grant, wire::DatagramType::kAudio,
                    1, 1, opus, sizeof(opus), datagram);
    datagram[size - 1] ^= 1;
    assert(session.Receive(Server(), datagram.data(), size, 1000) ==
           AdmissionResult::kAuthenticationFailed);
    datagram[size - 1] ^= 1;
    assert(session.Receive(Server(), datagram.data(), size, 1000) ==
           AdmissionResult::kAcceptedAudio);
    assert(session.last_authenticated_receive_us() == 1000);
    assert(session.PopPlayout(1000).kind == PlayoutKind::kAudio);
    assert(session.Receive(Server(), datagram.data(), size, 1000) == AdmissionResult::kReplay);

    // Authenticated stale media advances replay admission but never reaches playout.
    size = Downlink(downlink, grant, wire::DatagramType::kAudio,
                    2, 0, opus, sizeof(opus), datagram);
    assert(session.Receive(Server(), datagram.data(), size, 2000) ==
           AdmissionResult::kStaleGeneration);
    assert(session.Receive(Server(), datagram.data(), size, 2000) == AdmissionResult::kReplay);

    // The canonical forward window is 1024 packets; a larger jump is rejected.
    size = Downlink(downlink, grant, wire::DatagramType::kAudio,
                    1027, 1, opus, sizeof(opus), datagram);
    assert(session.Receive(Server(), datagram.data(), size, 2000) == AdmissionResult::kReplay);

    std::array<uint8_t, wire::kMaxDatagramBytes + 1> oversize{};
    assert(session.Receive(Server(), oversize.data(), oversize.size(), 0) ==
           AdmissionResult::kInvalidFraming);

    Endpoint wrong = Server(); wrong.port++;
    size = Downlink(downlink, grant, wire::DatagramType::kAudio,
                    3, 1, opus, sizeof(opus), datagram);
    assert(session.Receive(wrong, datagram.data(), size, 2000) == AdmissionResult::kWrongSource);
    assert(session.Receive(Server(), datagram.data(), size, 2000) ==
           AdmissionResult::kAcceptedAudio);

    // AAD tampering is rejected before it can alter the generation fence.
    size = Downlink(downlink, grant, wire::DatagramType::kAudio,
                    4, 1, opus, sizeof(opus), datagram);
    datagram[27] ^= 1;
    assert(session.Receive(Server(), datagram.data(), size, 3000) ==
           AdmissionResult::kAuthenticationFailed);
    datagram[27] ^= 1;
    assert(session.Receive(Server(), datagram.data(), size, 3000) ==
           AdmissionResult::kAcceptedAudio);

    // Reordering waits for the missing sequence 2 before releasing 3 and 4.
    assert(session.PopPlayout(3000).kind == PlayoutKind::kNone);
    const auto first_plc = session.PopPlayout(123001);
    assert(first_plc.kind == PlayoutKind::kPlc);
    assert(first_plc.arrived_us == 2000);
    assert(session.PopPlayout(123001).kind == PlayoutKind::kAudio);
    assert(session.PopPlayout(123001).kind == PlayoutKind::kAudio);

    // A later gap exposes an explicit PLC frame after the 120 ms deadline.
    size = Downlink(downlink, grant, wire::DatagramType::kAudio,
                    6, 1, opus, sizeof(opus), datagram);
    assert(session.Receive(Server(), datagram.data(), size, 130000) ==
           AdmissionResult::kAcceptedAudio);
    assert(session.PopPlayout(130000).kind == PlayoutKind::kNone);
    const auto later_plc = session.PopPlayout(250001);
    assert(later_plc.kind == PlayoutKind::kPlc);
    assert(later_plc.arrived_us == 130000);
    assert(session.PopPlayout(250001).kind == PlayoutKind::kAudio);

    // Authenticated future media is bounded and remains silent until control advances.
    for (uint32_t sequence : {9U, 7U, 8U, 10U}) {
        size = Downlink(downlink, grant, wire::DatagramType::kAudio,
                        sequence, 2, opus, sizeof(opus), datagram);
        assert(session.Receive(Server(), datagram.data(), size, 260000 + sequence) ==
               AdmissionResult::kFutureGeneration);
    }
    assert(session.generation() == 1);
    assert(UdpSessionTestPeer::BufferedCount(session) == 4);
    assert(session.PopPlayout(500000).kind == PlayoutKind::kNone);

    size = Downlink(downlink, grant, wire::DatagramType::kAudio,
                    11, 2, opus, sizeof(opus), datagram);
    assert(session.Receive(Server(), datagram.data(), size, 270000) ==
           AdmissionResult::kQueueFull);
    assert(session.Receive(Server(), datagram.data(), size, 270000) ==
           AdmissionResult::kReplay);

    assert(UdpSessionTestPeer::AdvanceGeneration(&session, 2));
    assert(session.generation() == 2);
    assert(UdpSessionTestPeer::BufferedCount(session) == 0);
    assert(UdpSessionTestPeer::BuffersAreZero(session));
    for (int index = 0; index < 4; ++index) {
        const auto frame = session.PopPlayout(500000);
        assert(frame.kind == PlayoutKind::kAudio);
        assert(frame.generation == 2);
        assert(frame.payload_size == sizeof(opus));
        assert(std::equal(frame.payload.begin(), frame.payload.begin() + sizeof(opus), opus));
    }

    const Stats stats = session.stats();
    assert(stats.authentication_failed == 2);
    assert(stats.wrong_source == 1);
    assert(stats.stale_generation == 1);
    assert(stats.future_generation == 5);
    assert(stats.lost == 2);
    assert(stats.played == 8);
    assert(stats.queue_dropped == 1);

    // A generation jump drops stale buffered media and retains only exact/newer generations.
    size = Downlink(downlink, grant, wire::DatagramType::kAudio,
                    12, 3, opus, sizeof(opus), datagram);
    assert(session.Receive(Server(), datagram.data(), size, 300000) ==
           AdmissionResult::kFutureGeneration);
    size = Downlink(downlink, grant, wire::DatagramType::kAudio,
                    13, 4, opus, sizeof(opus), datagram);
    assert(session.Receive(Server(), datagram.data(), size, 300001) ==
           AdmissionResult::kFutureGeneration);
    assert(UdpSessionTestPeer::AdvanceGeneration(&session, 4));
    assert(UdpSessionTestPeer::BufferedCount(session) == 0);
    assert(UdpSessionTestPeer::BuffersAreZero(session));
    const auto generation_four = session.PopPlayout(300001);
    assert(generation_four.kind == PlayoutKind::kAudio && generation_four.generation == 4);

    // Reconfiguration cannot expose payload retained by the previous session.
    size = Downlink(downlink, grant, wire::DatagramType::kAudio,
                    14, 5, opus, sizeof(opus), datagram);
    assert(session.Receive(Server(), datagram.data(), size, 310000) ==
           AdmissionResult::kFutureGeneration);
    assert(session.Configure(grant));
    assert(session.last_authenticated_receive_us() == 0);
    assert(UdpSessionTestPeer::BufferedCount(session) == 0);
    assert(UdpSessionTestPeer::BuffersAreZero(session));

    // Control datagrams consume wire sequence without creating false audio loss.
    FakeAead cadence_uplink;
    FakeAead cadence_downlink;
    UdpSession cadence(cadence_uplink, cadence_downlink);
    assert(cadence.Configure(grant));
    size = Downlink(cadence_downlink, grant, wire::DatagramType::kProbeAck,
                    0, 1, nullptr, 0, datagram);
    assert(cadence.Receive(Server(), datagram.data(), size, 0) ==
           AdmissionResult::kAcceptedProbeAck);
    assert(UdpSessionTestPeer::AdvanceGeneration(&cadence, 1));
    size = Downlink(cadence_downlink, grant, wire::DatagramType::kAudio,
                    1, 1, opus, sizeof(opus), datagram);
    assert(cadence.Receive(Server(), datagram.data(), size, 1000) ==
           AdmissionResult::kAcceptedAudio);
    size = Downlink(cadence_downlink, grant, wire::DatagramType::kKeepalive,
                    2, 1, nullptr, 0, datagram);
    assert(cadence.Receive(Server(), datagram.data(), size, 2000) ==
           AdmissionResult::kAcceptedKeepalive);
    size = Downlink(cadence_downlink, grant, wire::DatagramType::kAudio,
                    3, 1, opus, sizeof(opus), datagram);
    assert(cadence.Receive(Server(), datagram.data(), size, 3000) ==
           AdmissionResult::kAcceptedAudio);
    assert(cadence.PopPlayout(3000).kind == PlayoutKind::kAudio);
    assert(cadence.PopPlayout(3000).kind == PlayoutKind::kAudio);
    assert(cadence.PopPlayout(200000).kind == PlayoutKind::kNone);
    assert(cadence.stats().lost == 0);

    // Initial and post-cancel audio can beat response.begin over UDP. Neither
    // generation becomes playable until the WSS control owner activates it.
    FakeAead fenced_uplink;
    FakeAead fenced_downlink;
    UdpSession fenced(fenced_uplink, fenced_downlink);
    assert(fenced.Configure(grant));
    size = Downlink(fenced_downlink, grant, wire::DatagramType::kProbeAck,
                    0, 1, nullptr, 0, datagram);
    assert(fenced.Receive(Server(), datagram.data(), size, 0) ==
           AdmissionResult::kAcceptedProbeAck);
    size = Downlink(fenced_downlink, grant, wire::DatagramType::kAudio,
                    1, 1, opus, sizeof(opus), datagram);
    assert(fenced.Receive(Server(), datagram.data(), size, 1000) ==
           AdmissionResult::kFutureGeneration);
    assert(fenced.PopPlayout(500000).kind == PlayoutKind::kNone);
    assert(UdpSessionTestPeer::AdvanceGeneration(&fenced, 1));
    assert(fenced.PopPlayout(500000).kind == PlayoutKind::kAudio);

    size = Downlink(fenced_downlink, grant, wire::DatagramType::kAudio,
                    2, 2, opus, sizeof(opus), datagram);
    assert(fenced.Receive(Server(), datagram.data(), size, 510000) ==
           AdmissionResult::kFutureGeneration);
    assert(UdpSessionTestPeer::FenceGeneration(&fenced, 2));
    assert(fenced.PopPlayout(700000).kind == PlayoutKind::kNone);
    assert(UdpSessionTestPeer::AdvanceGeneration(&fenced, 2));
    assert(fenced.PopPlayout(700000).kind == PlayoutKind::kAudio);

    // Partial key configuration is rolled back and does not leave the uplink key live.
    FakeAead partial_uplink;
    FakeAead rejecting_downlink;
    rejecting_downlink.set_allow_key(false);
    UdpSession rejected(partial_uplink, rejecting_downlink);
    assert(!rejected.Configure(grant));
    assert(!partial_uplink.keyed() && !rejecting_downlink.keyed());
    assert(partial_uplink.clear_count() >= 2 && rejecting_downlink.clear_count() >= 2);

    FakeAead raii_uplink;
    FakeAead raii_downlink;
    {
        UdpSession scoped(raii_uplink, raii_downlink);
        assert(scoped.Configure(grant));
    }
    assert(!raii_uplink.keyed() && !raii_downlink.keyed());
    return 0;
}
