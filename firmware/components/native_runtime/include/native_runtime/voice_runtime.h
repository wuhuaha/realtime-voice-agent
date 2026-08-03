#pragma once

#include <array>
#include <atomic>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#include <freertos/FreeRTOS.h>
#include <freertos/event_groups.h>
#include <freertos/queue.h>
#include <freertos/semphr.h>
#include <freertos/task.h>

#include "audio_frontend_esp_sr/esp_sr_frontend.h"
#include "audio_pipeline/audio_pipeline.h"
#include "native_runtime/director_bootstrap.h"
#include "native_runtime/opus_codec.h"
#include "native_runtime/playback_state.h"
#include "native_runtime/udp_socket_port.h"
#include "native_runtime/uplink_pipeline.h"
#include "transport_udp/gcm_crypto.h"
#include "transport_udp/udp_runtime.h"
#include "transport_udp/udp_session.h"
#include "transport_wss/esp_websocket_client_port.h"
#include "transport_wss/wss_session.h"
#include "voice_core/session_gate.h"

namespace rva::runtime {

enum class MediaPreference : uint8_t { kWss, kUdp };
enum class ConversationPhase : uint8_t { kListening, kThinking, kSpeaking };

struct VoiceRuntimeConfig final {
    bool aec = false;
    bool vad = false;
    bool wake_word = false;
    bool display = false;
    bool touch = false;
};

class RuntimeEventSink {
public:
    virtual ~RuntimeEventSink() = default;
    virtual void OnConnection(bool connected) = 0;
    virtual void OnMediaProfile(MediaPreference preference) = 0;
    virtual void OnTranscript(const char* text, bool final) = 0;
    virtual void OnResponseText(const char* text) = 0;
    virtual void OnConversationPhase(ConversationPhase phase) = 0;
    virtual void OnFailure(const char* category) = 0;
};

class NullRuntimeEventSink final : public RuntimeEventSink {
public:
    void OnConnection(bool) override {}
    void OnMediaProfile(MediaPreference) override {}
    void OnTranscript(const char*, bool) override {}
    void OnResponseText(const char*) override {}
    void OnConversationPhase(ConversationPhase) override {}
    void OnFailure(const char*) override {}
};

class VoiceRuntime final {
public:
    using FailClosedHook = void (*)(void*) noexcept;

    VoiceRuntime(
        audio::AudioPipeline& pipeline,
        audio::EspSrFrontend& frontend,
        RuntimeEventSink& events,
        VoiceRuntimeConfig config = {});
    ~VoiceRuntime();

    bool Start(const BootstrapGrant& grant, const std::string& device_id,
               MediaPreference preference = MediaPreference::kWss);
    void Stop();
    void SetFailClosedHook(FailClosedHook hook, void* context) noexcept {
        fail_closed_hook_ = hook;
        fail_closed_context_ = context;
    }
    bool running() const { return running_; }
    bool media_ready() const {
        if (!session_opened_.load() || !media_started_.load()) return false;
        return media_owner_.load() != voice::core::MediaOwner::kUdp ||
               (udp_heartbeat_interval_us_.load() > 0 && udp_liveness_timeout_us_.load() > 0);
    }
    bool should_refresh_session() const { return udp_refresh_requested_.load(); }
    bool should_fallback_to_wss() const { return fallback_to_wss_.load(); }

private:
    struct MediaPacket final {
        uint16_t size = 0;
        uint32_t sequence = 0;
        uint32_t generation = 0;
        std::array<uint8_t, protocol::kWssMaxPayloadBytes> bytes{};
    };

    enum class PlaybackCommandType : uint8_t { kBegin, kComplete, kStop };

    struct PlaybackCommand final {
        PlaybackCommandType type = PlaybackCommandType::kBegin;
        std::array<char, protocol::kMaxIdBytes + 1> response_id{};
        uint32_t generation = 0;
        uint32_t value = 0;
    };

    struct QueuedPlaybackFact final {
        PlaybackFactType type = PlaybackFactType::kStarted;
        std::array<char, protocol::kMaxIdBytes + 1> response_id{};
        uint32_t generation = 0;
        protocol::PlaybackEndedOutcome outcome = protocol::PlaybackEndedOutcome::kCompleted;
        uint64_t played_samples = 0;
        uint32_t media_sequence = 0;
        bool has_media_sequence = false;
    };

    struct EncodedUplinkFrame final {
        uint16_t size = 0;
        uint32_t timestamp = 0;
        int64_t captured_at_us = 0;
        std::array<uint8_t, protocol::kWssMaxPayloadBytes> bytes{};
    };

    struct StageCounters final {
        std::atomic<uint32_t> count{0};
        std::atomic<uint32_t> total_us{0};
        std::atomic<uint32_t> max_us{0};
        std::atomic<uint32_t> deadline_misses{0};
    };

    struct WebsocketTeardownContext final {
        wss::WssOwner* owner = nullptr;
        SemaphoreHandle_t done = nullptr;
        bool result = false;
    };

    static void SupervisorTask(void* context);
    static void CaptureTask(void* context);
    static void UplinkFramerTask(void* context);
    static void UplinkEncoderTask(void* context);
    static void UplinkSenderTask(void* context);
    static void PlaybackTask(void* context);
    static void WebsocketTeardownTask(void* context);
    static void NotifySupervisorWork(void* context) noexcept;
    void RunSupervisor();
    void RunCapture();
    void RunUplinkFramer();
    void RunUplinkEncoder();
    void RunUplinkSender();
    void RunPlayback();
    void HandleControl(const std::vector<uint8_t>& frame);
    void HandleMedia(const std::vector<uint8_t>& frame);
    bool EnqueuePlaybackCommand(
        PlaybackCommandType type,
        const protocol::ResponseTarget& target,
        uint32_t value);
    bool ProcessPlaybackCommands();
    bool PublishPlaybackFact(const PlaybackFact& fact);
    bool DrainPlaybackFacts();
    bool SendSessionOpen();
    bool ConfigureUdp(const protocol::SessionOpened& opened);
    bool StartMediaRuntime();
    void StopMediaRuntime();
    bool StartPlaybackResampler();
    void StopPlaybackResampler();
    void RecordStage(StageCounters* counters, uint32_t duration_us, uint32_t deadline_us);
    void LogAndResetUplinkMetrics();
    void UpdateQueueHighWater(std::atomic<uint32_t>* high_water, QueueHandle_t queue);
    void MarkTaskStopped(EventBits_t bit);
    bool CloseWebsocketBounded(uint32_t timeout_ms);
    [[noreturn]] void FailClosedRestart(const char* category) noexcept;
    [[noreturn]] void HandleTaskAllocationFailure(
        const char* task_name, EventBits_t stopped_bit, bool stack_uses_caps) noexcept;

    audio::AudioPipeline& pipeline_;
    audio::EspSrFrontend& frontend_;
    RuntimeEventSink& events_;
    VoiceRuntimeConfig config_;
    OpusCodec codec_;
    void* playback_resampler_ = nullptr;
    std::unique_ptr<int16_t[]> playback_pcm_;
    std::unique_ptr<int16_t[]> playback_resampled_;
    size_t playback_resampled_capacity_ = 0;
    std::unique_ptr<wss::EspIdfWebsocketClientPort> client_port_;
    std::unique_ptr<wss::WssOwner> owner_;
    std::unique_ptr<wss::WssSession> session_;
    std::unique_ptr<udp::MbedTlsGcm> udp_uplink_crypto_;
    std::unique_ptr<udp::MbedTlsGcm> udp_downlink_crypto_;
    std::unique_ptr<udp::UdpSession> udp_session_;
    std::unique_ptr<UdpSocketPort> udp_io_;
    std::unique_ptr<udp::UdpRuntime> udp_runtime_;
    wss::FrameAssembler assembler_;
    voice::core::SessionGate core_gate_;
    QueueHandle_t playback_queue_ = nullptr;
    QueueHandle_t playback_command_queue_ = nullptr;
    QueueHandle_t playback_fact_queue_ = nullptr;
    QueueHandle_t uplink_pcm_queue_ = nullptr;
    QueueHandle_t uplink_encoded_queue_ = nullptr;
    EventGroupHandle_t task_events_ = nullptr;
    SemaphoreHandle_t supervisor_work_signal_ = nullptr;
    TaskHandle_t supervisor_task_ = nullptr;
    TaskHandle_t capture_task_ = nullptr;
    TaskHandle_t uplink_framer_task_ = nullptr;
    TaskHandle_t uplink_encoder_task_ = nullptr;
    TaskHandle_t uplink_sender_task_ = nullptr;
    TaskHandle_t playback_task_ = nullptr;
    // Supervisor is the only media-task creator. Stop first joins supervisor,
    // then reads this final mask and joins every media owner before teardown.
    std::atomic<EventBits_t> expected_task_bits_{0};
    std::mutex identity_mutex_;
    PlaybackState playback_state_;
    protocol::SessionOpened opened_{};
    std::string device_id_;
    std::string open_request_id_;
    std::string expected_session_epoch_;
    std::string authorization_headers_;
    std::atomic<bool> running_{false};
    std::atomic<bool> started_{false};
    std::atomic<bool> websocket_started_{false};
    std::atomic<bool> media_started_{false};
    std::atomic<bool> session_opened_{false};
    std::atomic<int64_t> session_open_deadline_us_{0};
    std::atomic<MediaPreference> preferred_media_{MediaPreference::kWss};
    std::atomic<voice::core::MediaOwner> media_owner_{voice::core::MediaOwner::kNone};
    std::atomic<uint32_t> playback_generation_{1};
    std::atomic<bool> playback_enabled_{false};
    // Derived from the authenticated refresh_after_ms control value and the
    // local monotonic timer. This is intentionally not a wall-clock expiry.
    std::atomic<int64_t> udp_refresh_deadline_us_{0};
    std::atomic<int64_t> udp_heartbeat_interval_us_{0};
    std::atomic<int64_t> udp_liveness_timeout_us_{0};
    std::atomic<int64_t> udp_next_keepalive_us_{0};
    std::atomic<bool> udp_refresh_requested_{false};
    std::atomic<bool> fallback_to_wss_{false};
    std::atomic<uint32_t> wss_playback_queue_dropped_{0};
    UplinkFramer uplink_framer_;
    StageCounters capture_stage_;
    StageCounters framing_stage_;
    StageCounters encode_stage_;
    StageCounters send_stage_;
    std::atomic<uint32_t> uplink_pcm_queue_dropped_{0};
    std::atomic<uint32_t> uplink_encoded_queue_dropped_{0};
    std::atomic<uint32_t> uplink_pcm_queue_high_water_{0};
    std::atomic<uint32_t> uplink_encoded_queue_high_water_{0};
    std::atomic<uint32_t> uplink_pcm_max_age_us_{0};
    std::atomic<uint32_t> uplink_encoded_max_age_us_{0};
    std::atomic<uint32_t> uplink_local_send_completion_max_age_us_{0};
    std::atomic<uint32_t> uplink_presend_stale_dropped_{0};
    std::atomic<uint32_t> wss_uplink_send_failures_{0};
    FailClosedHook fail_closed_hook_ = nullptr;
    void* fail_closed_context_ = nullptr;
    uint32_t uplink_sequence_ = 0;
};

}  // namespace rva::runtime
