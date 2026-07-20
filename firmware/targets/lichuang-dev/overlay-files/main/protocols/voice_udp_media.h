#pragma once

#include "protocol.h"
#include "voice_udp_wire.h"

#include <array>
#include <atomic>
#include <cstdint>
#include <functional>
#include <memory>
#include <mutex>
#include <string>

#include <cJSON.h>
#include <freertos/FreeRTOS.h>
#include <freertos/event_groups.h>
#include <freertos/task.h>
#include <mbedtls/gcm.h>
#include <udp.h>

class VoiceUdpMedia {
public:
    VoiceUdpMedia();
    ~VoiceUdpMedia();

    bool Configure(const cJSON* udp);
    bool Start();
    void RequestStop();
    bool SendAudio(const AudioStreamPacket& packet, uint32_t generation);
    bool IsReady() const;
    bool SetGeneration(uint32_t generation);
    void OnAudio(std::function<void(std::unique_ptr<AudioStreamPacket>)> callback);

private:
    static constexpr size_t kKeyBytes = 16;
    static constexpr size_t kMaxPayloadBytes = voice_udp_wire::kMaxPayloadBytes;
    static constexpr size_t kReorderSlots = 8;
    static constexpr uint32_t kMaxForwardSequenceDistance = 32;
    static constexpr uint32_t kMaxConcealmentPerPass = 2;
    static constexpr int kReorderWaitMs = 40;
    static constexpr int kProbeAttempts = 6;
    static constexpr int kProbeAttemptWaitMs = 500;
    static constexpr int kSendAttempts = 6;
    static constexpr int kSendRetryWaitMs = 2;
    static constexpr int kWakePreRollPacingMs = 4;
    static constexpr EventBits_t kProbeAckEvent = BIT0;
    static constexpr EventBits_t kTaskExitedEvent = BIT1;
    static constexpr EventBits_t kStopRequestedEvent = BIT2;
    static constexpr EventBits_t kDataReadyEvent = BIT3;

    struct PacketSlot {
        bool used = false;
        uint8_t flags = 0;
        uint32_t sequence = 0;
        uint32_t timestamp = 0;
        uint32_t generation = 0;
        uint16_t payload_size = 0;
        int64_t arrived_us = 0;
        std::array<uint8_t, kMaxPayloadBytes> payload{};
    };

    std::string host_;
    int port_ = 0;
    uint32_t media_epoch_ = 0;
    std::array<uint8_t, voice_udp_wire::kMediaIdBytes> media_id_{};
    std::array<uint8_t, voice_udp_wire::kSaltBytes> uplink_salt_{};
    std::array<uint8_t, voice_udp_wire::kSaltBytes> downlink_salt_{};
    mbedtls_gcm_context uplink_gcm_{};
    mbedtls_gcm_context downlink_gcm_{};
    std::unique_ptr<Udp> udp_;
    std::string send_buffer_;
    EventGroupHandle_t events_ = nullptr;
    std::atomic<TaskHandle_t> reorder_task_{nullptr};
    std::atomic<bool> revoked_{false};
    std::atomic<bool> running_{false};
    std::atomic<bool> ready_{false};
    std::atomic<uint32_t> generation_{1};
    std::function<void(std::unique_ptr<AudioStreamPacket>)> on_audio_;
    std::mutex lifecycle_mutex_;
    std::mutex send_mutex_;
    std::mutex slots_mutex_;
    bool start_attempted_ = false;
    bool cleanup_complete_ = false;
    std::array<PacketSlot, kReorderSlots> slots_{};
    uint32_t send_sequence_ = 0;
    uint32_t receive_highest_ = 0;
    uint64_t receive_bitmap_ = 0;
    uint32_t expected_audio_sequence_ = 1;
    bool reorder_cursor_initialized_ = false;
    uint32_t received_ = 0;
    uint32_t invalid_ = 0;
    uint32_t replayed_ = 0;
    uint32_t lost_ = 0;
    uint32_t queue_dropped_ = 0;
    uint32_t played_ = 0;
    uint32_t concealed_ = 0;
    uint32_t tx_crypto_samples_ = 0;
    uint32_t rx_crypto_samples_ = 0;
    uint64_t tx_crypto_us_total_ = 0;
    uint64_t rx_crypto_us_total_ = 0;
    uint32_t tx_crypto_us_max_ = 0;
    uint32_t rx_crypto_us_max_ = 0;
    UBaseType_t reorder_stack_min_words_ = UINT32_MAX;

    bool SendPacket(uint8_t flags, const uint8_t* payload, size_t payload_size,
                    uint32_t timestamp, uint32_t generation);
    void Stop();
    void HandleDatagram(const std::string& datagram);
    bool AcceptSequence(uint32_t sequence) const;
    void CommitSequence(uint32_t sequence);
    void AdvanceGenerationLocked(uint32_t generation);
    void InsertPacketLocked(uint8_t flags, uint32_t sequence, uint32_t timestamp,
                            uint32_t generation, const uint8_t* payload,
                            size_t payload_size);
    void ResetReorderCursor(uint32_t sequence);
    void ReorderLoop();
    bool PopExpected(PacketSlot& output);
    bool PopExpiredLoss(uint32_t& timestamp);
    void ClearSlots();
    void ClearSlotsLocked();
    static void ReorderTask(void* context);
    static bool DecodeHex(const cJSON* value, uint8_t* output, size_t output_size);
    static bool DecodeBase64(const cJSON* value, uint8_t* output, size_t output_size);
    static bool ParseExactUint32(const cJSON* value, uint32_t& output);
};
