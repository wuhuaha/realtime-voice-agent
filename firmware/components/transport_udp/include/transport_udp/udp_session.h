#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <mutex>

#include "transport_udp/crypto.h"
#include "transport_udp/jitter_buffer.h"
#include "transport_udp/replay_window.h"

namespace rva::udp {

class UdpRuntime;
class UdpSessionTestPeer;

class UdpSession final {
public:
    // Both crypto ports must outlive the session; destruction revokes the
    // session and clears both keyed contexts.
    UdpSession(AeadPort& uplink_crypto, AeadPort& downlink_crypto);
    ~UdpSession();
    bool Configure(const SessionGrant& grant);
    bool BuildProbe(uint8_t* output, size_t capacity, size_t* size);
    bool BuildAudio(const uint8_t* opus, size_t opus_size, uint32_t timestamp,
                    uint32_t generation, uint8_t* output, size_t capacity, size_t* size);
    AdmissionResult Receive(const Endpoint& source, const uint8_t* datagram,
                            size_t size, int64_t now_us);
    PlayoutFrame PopPlayout(int64_t now_us);
    [[nodiscard]] bool ready() const;
    [[nodiscard]] uint32_t generation() const;
    [[nodiscard]] Stats stats() const;
    [[nodiscard]] Endpoint server() const;
private:
    friend class UdpRuntime;
    friend class UdpSessionTestPeer;
    static constexpr size_t kFutureFrameCount = 4;
    struct FutureFrame final {
        bool used = false;
        uint32_t sequence = 0;
        uint32_t timestamp = 0;
        uint32_t generation = 0;
        uint16_t payload_size = 0;
        int64_t arrived_us = 0;
        std::array<uint8_t, wire::kMaxPayloadBytes> payload{};
    };
    void Revoke();
    bool FenceGeneration(uint32_t generation);
    bool AdvanceGeneration(uint32_t generation);
    void RevokeLocked();
    bool BufferFutureAudio(const wire::Header& header, int64_t now_us);
    void ReleaseFutureAudio(uint32_t generation);
    static void ClearFutureFrame(FutureFrame* frame);
    void ClearFutureFrames();
    bool Build(wire::DatagramType type, const uint8_t* payload, size_t payload_size,
               uint32_t timestamp, uint32_t generation, uint8_t* output,
               size_t capacity, size_t* size);
    AeadPort& uplink_crypto_;
    AeadPort& downlink_crypto_;
    SessionGrant grant_{};
    ReplayWindow replay_{};
    JitterBuffer jitter_{};
    std::array<FutureFrame, kFutureFrameCount> future_frames_{};
    std::array<uint8_t, wire::kMaxPayloadBytes> plaintext_{};
    Stats stats_{};
    Endpoint bound_source_{};
    uint32_t send_sequence_ = 0;
    uint32_t generation_ = 1;
    bool playback_active_ = false;
    bool configured_ = false;
    bool ready_ = false;
    bool revoked_ = true;
    mutable std::mutex mutex_;
};

}  // namespace rva::udp
