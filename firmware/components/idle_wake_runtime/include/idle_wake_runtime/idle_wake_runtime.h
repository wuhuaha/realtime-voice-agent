#pragma once

#include <atomic>
#include <cstdint>
#include <mutex>
#include <vector>

#include <freertos/FreeRTOS.h>
#include <freertos/event_groups.h>
#include <freertos/task.h>

#include "audio_frontend_esp_sr/esp_sr_frontend.h"
#include "audio_pipeline/audio_ports.h"

namespace rva::runtime {

enum class IdleWakeStartResult : uint8_t {
    kStarted = 0,
    kAlreadyStarted,
    kWakeModelUnavailable,
    kAudioStartFailure,
    kResourceExhausted,
};

// Owns capture and AFE only while the application is idle. The application
// must Stop() successfully before handing either port to VoiceRuntime.
class IdleWakeRuntime final {
public:
    IdleWakeRuntime(audio::CapturePort& capture, audio::EspSrFrontend& frontend);
    ~IdleWakeRuntime();

    IdleWakeRuntime(const IdleWakeRuntime&) = delete;
    IdleWakeRuntime& operator=(const IdleWakeRuntime&) = delete;

    IdleWakeStartResult Start();
    bool Stop(uint32_t timeout_ms = 2000);

    [[nodiscard]] bool started() const { return started_.load(); }
    [[nodiscard]] bool running() const { return running_.load(); }
    [[nodiscard]] bool failed() const { return failed_.load(); }
    bool ConsumeWakeDetection(uint32_t* wake_word_index = nullptr);

private:
    static void CaptureTask(void* context);
    static void FetchTask(void* context);
    void RunCapture();
    void RunFetch();
    void MarkStopped(EventBits_t bit);
    bool JoinTasks(uint32_t timeout_ms);
    bool StopAudioPorts();

    audio::CapturePort& capture_;
    audio::EspSrFrontend& frontend_;
    std::mutex lifecycle_mutex_;
    EventGroupHandle_t task_events_ = nullptr;
    TaskHandle_t capture_task_ = nullptr;
    TaskHandle_t fetch_task_ = nullptr;
    EventBits_t expected_task_bits_ = 0;
    std::vector<int16_t> capture_buffer_;
    std::vector<int16_t> fetch_buffer_;
    std::atomic<bool> started_{false};
    std::atomic<bool> running_{false};
    std::atomic<bool> failed_{false};
    std::atomic<uint32_t> wake_word_index_{0};
};

}  // namespace rva::runtime
