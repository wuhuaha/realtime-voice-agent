#pragma once

#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <mutex>

#include <freertos/FreeRTOS.h>
#include <freertos/event_groups.h>
#include <freertos/task.h>

#include "transport_udp/playout_queue.h"
#include "transport_udp/udp_session.h"

namespace rva::udp {

struct DatagramSendOutcome final {
    int sent_bytes = -1;
    int error_code = 0;
    uint32_t socket_generation = 0;
    uint16_t local_source_port = 0;
};

class DatagramIoPort {
public:
    virtual ~DatagramIoPort() = default;
    virtual DatagramSendOutcome Send(
        const Endpoint& destination, const uint8_t* data, size_t size) = 0;
    virtual bool Receive(uint8_t* data, size_t capacity, size_t* size,
                         Endpoint* source, uint32_t timeout_ms) = 0;
    virtual void Interrupt() = 0;
    virtual void Close() = 0;
};

// Owns only the UDP ingress/playout task and bounded playout queue. RequestStop
// is non-blocking; only the supervisor may call JoinAndClose and destroy it.
class UdpRuntime final {
public:
    // Six 60 ms Opus frames is the absolute freshness fence. Normal steady
    // state remains at one to two frames; this bound only prevents stale audio
    // from reaching decode/playback after scheduling or future-generation wait.
    static constexpr int64_t kMaximumMediaAgeUs = 360000;

    static void* operator new(size_t size);
    static void operator delete(void* pointer) noexcept;

    UdpRuntime(UdpSession& session, DatagramIoPort& io);
    ~UdpRuntime();
    bool Start();
    // Retries are byte-identical so the AEAD nonce/sequence-zero probe remains idempotent.
    // The server may re-ack this authenticated handshake packet without advancing replay state.
    bool SendProbe();
    bool SendKeepalive();
    bool SendAudio(const uint8_t* opus, size_t opus_size, uint32_t timestamp);
    bool FenceGeneration(uint32_t generation);
    bool AdvanceGeneration(uint32_t generation);
    bool PollPlayout(PlayoutFrame* frame);
    void RequestStop();
    bool JoinAndClose(uint32_t timeout_ms);
    [[nodiscard]] uint32_t playout_queue_dropped() const {
        return queue_dropped_.load();
    }
    [[nodiscard]] uint32_t playout_media_age_dropped() const {
        return media_age_dropped_.load();
    }
    [[nodiscard]] int64_t last_authenticated_receive_us() const {
        return session_.last_authenticated_receive_us();
    }
    [[nodiscard]] Stats stats() const { return session_.stats(); }
private:
    static constexpr EventBits_t kExited = BIT0;
    static void TaskEntry(void* context);
    void Run();
    bool SendDatagram(const uint8_t* data, size_t size, bool log_probe_outcome);

    UdpSession& session_;
    DatagramIoPort& io_;
    EventGroupHandle_t events_ = nullptr;
    PlayoutQueue playout_queue_{};
    std::atomic<TaskHandle_t> task_{nullptr};
    std::atomic<bool> stop_requested_{false};
    std::atomic<uint32_t> queue_dropped_{0};
    std::atomic<uint32_t> media_age_dropped_{0};
    std::atomic<bool> closed_{false};
    std::mutex send_mutex_;
    std::mutex control_mutex_;
};

}  // namespace rva::udp
