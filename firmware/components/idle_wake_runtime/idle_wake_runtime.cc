#include "idle_wake_runtime/idle_wake_runtime.h"

#include <cstdlib>
#include <new>

#include <esp_heap_caps.h>
#include <esp_log.h>
#include <freertos/idf_additions.h>

namespace rva::runtime {
namespace {

constexpr EventBits_t kCaptureStopped = BIT0;
constexpr EventBits_t kFetchStopped = BIT1;
constexpr uint32_t kCaptureTaskStackBytes = 8 * 1024;
constexpr uint32_t kFetchTaskStackBytes = 8 * 1024;
constexpr uint32_t kAudioFailureLimit = 10;
constexpr char kTag[] = "rva-idle-wake";

}  // namespace

IdleWakeRuntime::IdleWakeRuntime(
    audio::CapturePort& capture, audio::EspSrFrontend& frontend)
    : capture_(capture), frontend_(frontend) {}

IdleWakeRuntime::~IdleWakeRuntime() {
    if (!Stop()) {
        ESP_LOGE(kTag, "idle wake teardown timed out; aborting to prevent task use-after-free");
        std::abort();
    }
}

IdleWakeStartResult IdleWakeRuntime::Start() {
    std::lock_guard<std::mutex> lock(lifecycle_mutex_);
    if (started_.load()) return IdleWakeStartResult::kAlreadyStarted;

    failed_.store(false);
    wake_word_index_.store(0);
    expected_task_bits_ = 0;
    if (!frontend_.SetWakeNetEnabled(true)) {
        return IdleWakeStartResult::kAudioStartFailure;
    }
    if (frontend_.Start() != audio::PortResult::kOk) {
        StopAudioPorts();
        return IdleWakeStartResult::kAudioStartFailure;
    }
    if (!frontend_.wakenet_available()) {
        StopAudioPorts();
        return IdleWakeStartResult::kWakeModelUnavailable;
    }
    if (capture_.Start() != audio::PortResult::kOk) {
        StopAudioPorts();
        return IdleWakeStartResult::kAudioStartFailure;
    }

    try {
        capture_buffer_.resize(frontend_.feed_samples_per_channel() * 2);
        fetch_buffer_.resize(frontend_.fetch_samples_per_channel());
    } catch (const std::bad_alloc&) {
        StopAudioPorts();
        return IdleWakeStartResult::kResourceExhausted;
    }
    if (capture_buffer_.empty() || fetch_buffer_.empty()) {
        StopAudioPorts();
        return IdleWakeStartResult::kAudioStartFailure;
    }

    task_events_ = xEventGroupCreateWithCaps(MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (task_events_ == nullptr) {
        StopAudioPorts();
        return IdleWakeStartResult::kResourceExhausted;
    }
    running_.store(true);
    started_.store(true);
    if (xTaskCreateWithCaps(
            CaptureTask, "rva-wake-feed", kCaptureTaskStackBytes, this, 7,
            &capture_task_, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT) != pdPASS) {
        running_.store(false);
        started_.store(false);
        StopAudioPorts();
        vEventGroupDeleteWithCaps(task_events_);
        task_events_ = nullptr;
        return IdleWakeStartResult::kResourceExhausted;
    }
    expected_task_bits_ |= kCaptureStopped;
    if (xTaskCreateWithCaps(
            FetchTask, "rva-wake-fetch", kFetchTaskStackBytes, this, 6,
            &fetch_task_, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT) != pdPASS) {
        running_.store(false);
        if (!JoinTasks(1000)) {
            failed_.store(true);
            return IdleWakeStartResult::kResourceExhausted;
        }
        started_.store(false);
        StopAudioPorts();
        vEventGroupDeleteWithCaps(task_events_);
        task_events_ = nullptr;
        return IdleWakeStartResult::kResourceExhausted;
    }
    expected_task_bits_ |= kFetchStopped;
    ESP_LOGI(kTag, "idle WakeNet runtime started");
    return IdleWakeStartResult::kStarted;
}

bool IdleWakeRuntime::Stop(uint32_t timeout_ms) {
    std::lock_guard<std::mutex> lock(lifecycle_mutex_);
    if (!started_.load()) return true;
    running_.store(false);
    if (!JoinTasks(timeout_ms)) {
        failed_.store(true);
        ESP_LOGE(kTag, "idle WakeNet task join timed out bits=0x%lx",
                 static_cast<unsigned long>(expected_task_bits_));
        return false;
    }
    const bool audio_stopped = StopAudioPorts();
    if (task_events_ != nullptr) {
        vEventGroupDeleteWithCaps(task_events_);
        task_events_ = nullptr;
    }
    capture_task_ = nullptr;
    fetch_task_ = nullptr;
    expected_task_bits_ = 0;
    started_.store(false);
    ESP_LOGI(kTag, "idle WakeNet runtime stopped");
    return audio_stopped;
}

bool IdleWakeRuntime::ConsumeWakeDetection(uint32_t* wake_word_index) {
    const uint32_t detected = wake_word_index_.exchange(0);
    if (detected == 0) return false;
    if (wake_word_index != nullptr) *wake_word_index = detected;
    return true;
}

void IdleWakeRuntime::CaptureTask(void* context) {
    static_cast<IdleWakeRuntime*>(context)->RunCapture();
}

void IdleWakeRuntime::FetchTask(void* context) {
    static_cast<IdleWakeRuntime*>(context)->RunFetch();
}

void IdleWakeRuntime::RunCapture() {
    uint32_t consecutive_failures = 0;
    while (running_.load()) {
        audio::MutablePcmView input{
            .samples = capture_buffer_.data(),
            .capacity_samples = capture_buffer_.size(),
        };
        const audio::PortResult read = capture_.Read(&input, 100);
        if (read == audio::PortResult::kOk) {
            const audio::PortResult fed = frontend_.Feed({
                input.samples, input.sample_count, input.sample_rate_hz, input.channel_count});
            consecutive_failures = fed == audio::PortResult::kOk ? 0 : consecutive_failures + 1;
            vTaskDelay(1);
        } else if (read != audio::PortResult::kTimeout) {
            ++consecutive_failures;
        }
        if (consecutive_failures >= kAudioFailureLimit) {
            failed_.store(true);
            running_.store(false);
        }
    }
    ESP_LOGI(kTag, "wake feed minimum free stack: %lu bytes",
             static_cast<unsigned long>(uxTaskGetStackHighWaterMark(nullptr)));
    MarkStopped(kCaptureStopped);
    vTaskDeleteWithCaps(nullptr);
}

void IdleWakeRuntime::RunFetch() {
    uint32_t consecutive_failures = 0;
    while (running_.load()) {
        audio::MutablePcmView output{
            .samples = fetch_buffer_.data(),
            .capacity_samples = fetch_buffer_.size(),
        };
        const audio::PortResult fetched = frontend_.Fetch(&output, 100);
        if (fetched == audio::PortResult::kOk) {
            consecutive_failures = 0;
            uint32_t index = 0;
            if (frontend_.ConsumeWakeDetection(&index)) {
                wake_word_index_.store(index);
                running_.store(false);
                ESP_LOGI(kTag, "wake word detected index=%lu", static_cast<unsigned long>(index));
            }
        } else if (fetched != audio::PortResult::kTimeout) {
            ++consecutive_failures;
        }
        if (consecutive_failures >= kAudioFailureLimit) {
            failed_.store(true);
            running_.store(false);
        }
    }
    ESP_LOGI(kTag, "wake fetch minimum free stack: %lu bytes",
             static_cast<unsigned long>(uxTaskGetStackHighWaterMark(nullptr)));
    MarkStopped(kFetchStopped);
    vTaskDeleteWithCaps(nullptr);
}

void IdleWakeRuntime::MarkStopped(EventBits_t bit) {
    if (task_events_ != nullptr) xEventGroupSetBits(task_events_, bit);
}

bool IdleWakeRuntime::JoinTasks(uint32_t timeout_ms) {
    if (task_events_ == nullptr || expected_task_bits_ == 0) return true;
    const EventBits_t stopped = xEventGroupWaitBits(
        task_events_, expected_task_bits_, pdFALSE, pdTRUE, pdMS_TO_TICKS(timeout_ms));
    return (stopped & expected_task_bits_) == expected_task_bits_;
}

bool IdleWakeRuntime::StopAudioPorts() {
    const bool capture_stopped = capture_.Stop() == audio::PortResult::kOk;
    const bool frontend_stopped = frontend_.Stop() == audio::PortResult::kOk;
    const bool wakenet_disabled = frontend_.SetWakeNetEnabled(false);
    if (!capture_stopped || !frontend_stopped || !wakenet_disabled) {
        ESP_LOGE(kTag, "failed to stop idle audio cleanly capture=%d frontend=%d wakenet=%d",
                 capture_stopped, frontend_stopped, wakenet_disabled);
        failed_.store(true);
    }
    capture_buffer_.clear();
    fetch_buffer_.clear();
    return capture_stopped && frontend_stopped && wakenet_disabled;
}

}  // namespace rva::runtime
