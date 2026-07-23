#pragma once

#include <array>
#include <atomic>
#include <memory>
#include <mutex>
#include <string>

#include <freertos/FreeRTOS.h>
#include <freertos/event_groups.h>
#include <freertos/queue.h>
#include <freertos/semphr.h>
#include <freertos/task.h>

#include "audio_frontend_esp_sr/esp_sr_frontend.h"
#include "audio_pipeline/audio_pipeline.h"
#include "native_runtime/director_bootstrap.h"
#include "native_runtime/opus_codec.h"
#include "native_runtime/response_interrupt_gate.h"
#include "native_runtime/udp_socket_port.h"
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
    bool should_fallback_to_wss() const { return fallback_to_wss_.load(); }

private:
    struct MediaPacket final {
        uint16_t size = 0;
        uint32_t generation = 0;
        std::array<uint8_t, protocol::kWssMaxPayloadBytes> bytes{};
    };

    struct WebsocketTeardownContext final {
        wss::WssOwner* owner = nullptr;
        SemaphoreHandle_t done = nullptr;
        bool result = false;
    };

    static void SupervisorTask(void* context);
    static void CaptureTask(void* context);
    static void UplinkTask(void* context);
    static void PlaybackTask(void* context);
    static void WebsocketTeardownTask(void* context);
    void RunSupervisor();
    void RunCapture();
    void RunUplink();
    void RunPlayback();
    void HandleControl(const std::vector<uint8_t>& frame);
    void HandleMedia(const std::vector<uint8_t>& frame);
    bool CancelActiveResponseOnSpeech();
    bool CompleteResponseDrainIfDue(int64_t now_us);
    bool SendSessionOpen();
    bool ConfigureUdp(const protocol::SessionOpened& opened);
    bool StartMediaRuntime();
    void StopMediaRuntime();
    bool StartPlaybackResampler();
    void StopPlaybackResampler();
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
    EventGroupHandle_t task_events_ = nullptr;
    TaskHandle_t supervisor_task_ = nullptr;
    TaskHandle_t capture_task_ = nullptr;
    TaskHandle_t uplink_task_ = nullptr;
    TaskHandle_t playback_task_ = nullptr;
    EventBits_t expected_task_bits_ = 0;
    std::mutex identity_mutex_;
    std::mutex playback_mutex_;
    ResponseInterruptGate response_gate_;
    protocol::SessionOpened opened_{};
    std::string device_id_;
    std::string open_request_id_;
    std::string expected_session_epoch_;
    std::string authorization_headers_;
    std::atomic<bool> running_{false};
    std::atomic<bool> started_{false};
    std::atomic<bool> media_started_{false};
    std::atomic<bool> session_opened_{false};
    std::atomic<MediaPreference> preferred_media_{MediaPreference::kWss};
    std::atomic<voice::core::MediaOwner> media_owner_{voice::core::MediaOwner::kNone};
    std::atomic<uint32_t> playback_generation_{1};
    std::atomic<bool> playback_enabled_{false};
    std::atomic<int64_t> udp_expiry_deadline_us_{0};
    std::atomic<int64_t> udp_heartbeat_interval_us_{0};
    std::atomic<int64_t> udp_liveness_timeout_us_{0};
    std::atomic<int64_t> udp_next_keepalive_us_{0};
    std::atomic<int64_t> response_end_deadline_us_{0};
    std::atomic<bool> fallback_to_wss_{false};
    std::atomic<uint32_t> wss_playback_queue_dropped_{0};
    FailClosedHook fail_closed_hook_ = nullptr;
    void* fail_closed_context_ = nullptr;
    uint32_t uplink_sequence_ = 0;
    uint32_t uplink_timestamp_ = 0;
};

}  // namespace rva::runtime
