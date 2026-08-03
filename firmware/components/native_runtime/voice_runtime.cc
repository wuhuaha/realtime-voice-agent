#include "native_runtime/voice_runtime.h"

#include <algorithm>
#include <array>
#include <chrono>
#include <cstdlib>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <limits>
#include <new>
#include <variant>
#include <vector>

#include <esp_crt_bundle.h>
#include <esp_heap_caps.h>
#include <esp_ae_rate_cvt.h>
#include <esp_log.h>
#include <esp_random.h>
#include <esp_timer.h>
#include <esp_websocket_client.h>
#include <freertos/idf_additions.h>

#include "voice_contracts/transport_profile.h"

namespace rva::runtime {
namespace {

constexpr EventBits_t kSupervisorStopped = BIT0;
constexpr EventBits_t kCaptureStopped = BIT1;
constexpr EventBits_t kUplinkFramerStopped = BIT2;
constexpr EventBits_t kUplinkEncoderStopped = BIT3;
constexpr EventBits_t kUplinkSenderStopped = BIT4;
constexpr EventBits_t kPlaybackStopped = BIT5;
constexpr uint32_t kAudioFailureLimit = 10;
// HIL measured about 26 KiB used through the first Opus encode. Keep roughly
// 10 KiB of headroom while returning scarce internal RAM to the WSS client.
constexpr uint32_t kUplinkFramerTaskStackBytes = 8 * 1024;
constexpr uint32_t kUplinkEncoderTaskStackBytes = 36 * 1024;
constexpr uint32_t kUplinkSenderTaskStackBytes = 12 * 1024;
// HIL measured roughly 20 KiB unused with the former 24 KiB stack.
constexpr uint32_t kCaptureTaskStackBytes = 8 * 1024;
// A 10 KiB stack overflowed on the first Opus decode/resample pass in HIL.
// Keep internal-RAM headroom now and tune down only from measured high-water data.
constexpr uint32_t kPlaybackTaskStackBytes = 20 * 1024;
constexpr uint32_t kWebsocketTeardownStackBytes = 6 * 1024;
constexpr uint32_t kWebsocketCloseTimeoutMs = 1000;
constexpr uint32_t kWebsocketTeardownTimeoutMs = 1500;
// UDP media keeps the control WebSocket mostly idle while audio is flowing on
// the datagram path. A 10 s transport timeout is too close to normal Chinese
// TTS durations and caused the ESP client to disconnect near the end of long
// replies, which then made the auto-start loop reopen the session. Keep this
// comfortably above the server heartbeat/idle contract.
constexpr int kWebsocketNetworkTimeoutMs = 60000;
constexpr int kWebsocketPingIntervalSec = 10;
constexpr int64_t kSessionOpenDeadlineUs = 8 * 1000 * 1000;
constexpr int64_t kUdpProbeRetryIntervalUs = 250000;
constexpr size_t kDecodedSamplesPerFrame = 960;
constexpr size_t kNominalResampledSamplesPerFrame = 1440;
constexpr size_t kMaximumResampledSamplesPerFrame = 4096;
constexpr UBaseType_t kUplinkQueueCapacity = 2;
constexpr int64_t kUplinkMetricsIntervalUs = 10 * 1000 * 1000;
constexpr uint32_t kCaptureProcessingDeadlineUs = 16000;
constexpr uint32_t kFramingProcessingDeadlineUs = 16000;
constexpr uint32_t kEncodeDeadlineUs = 60000;
constexpr uint32_t kSendDeadlineUs = 60000;
constexpr char kLogTag[] = "rva-runtime";

BaseType_t CaptureTaskCore() {
#if CONFIG_RVA_UPLINK_AFFINITY_AUDIO_CPU1
    return 1;
#else
    return tskNO_AFFINITY;
#endif
}

BaseType_t FramerTaskCore() {
#if CONFIG_RVA_UPLINK_AFFINITY_AUDIO_CPU1
    return 1;
#else
    return tskNO_AFFINITY;
#endif
}

BaseType_t EncoderTaskCore() {
#if CONFIG_RVA_UPLINK_AFFINITY_OPUS_CPU1 || CONFIG_RVA_UPLINK_AFFINITY_AUDIO_CPU1
    return 1;
#else
    return tskNO_AFFINITY;
#endif
}

BaseType_t SenderTaskCore() {
#if CONFIG_RVA_UPLINK_AFFINITY_OPUS_CPU1 || CONFIG_RVA_UPLINK_AFFINITY_AUDIO_CPU1
    return 0;
#else
    return tskNO_AFFINITY;
#endif
}

void UpdateMaximum(std::atomic<uint32_t>* maximum, uint32_t value) {
    uint32_t observed = maximum->load(std::memory_order_relaxed);
    while (observed < value &&
           !maximum->compare_exchange_weak(
               observed, value, std::memory_order_relaxed, std::memory_order_relaxed)) {
    }
}

}  // namespace

VoiceRuntime::VoiceRuntime(
    audio::AudioPipeline& pipeline,
    audio::EspSrFrontend& frontend,
    RuntimeEventSink& events,
    VoiceRuntimeConfig config)
    : pipeline_(pipeline), frontend_(frontend), events_(events), config_(config) {}

VoiceRuntime::~VoiceRuntime() {
    Stop();
}

bool VoiceRuntime::Start(
    const BootstrapGrant& grant,
    const std::string& device_id,
    MediaPreference preference) {
    if (started_ || grant.worker_wss_url.empty() || grant.connect_grant.empty() || grant.session_epoch.empty() ||
        device_id.empty()) {
        ESP_LOGE(kLogTag, "start rejected: invalid precondition");
        return false;
    }
    device_id_ = device_id;
    preferred_media_ = preference;
    media_owner_ = voice::core::MediaOwner::kNone;
    playback_generation_ = 1;
    playback_enabled_ = false;
    udp_refresh_deadline_us_ = 0;
    udp_heartbeat_interval_us_ = 0;
    udp_liveness_timeout_us_ = 0;
    udp_next_keepalive_us_ = 0;
    udp_refresh_requested_ = false;
    fallback_to_wss_ = false;
    wss_playback_queue_dropped_ = 0;
    media_started_ = false;
    session_open_deadline_us_ = 0;
    playback_state_.Reset();
    expected_session_epoch_ = grant.session_epoch;
    std::array<char, 24> request{};
    std::snprintf(request.data(), request.size(), "open-%08lx", static_cast<unsigned long>(esp_random()));
    open_request_id_ = request.data();
    session_.reset(new (std::nothrow) wss::WssSession(open_request_id_));
    if (session_ == nullptr || !core_gate_.BeginFreshSession(1)) {
        ESP_LOGE(kLogTag, "start failed: session allocation or fresh-session gate");
        return false;
    }

    authorization_headers_ = "Authorization: Bearer " + grant.connect_grant + "\r\n" +
                             "Device-Id: " + device_id_ + "\r\n" +
                             "Client-Id: " + device_id_ + "\r\n";
    esp_websocket_client_config_t websocket{};
    websocket.uri = grant.worker_wss_url.c_str();
    websocket.headers = authorization_headers_.c_str();
    websocket.disable_auto_reconnect = true;
    websocket.network_timeout_ms = kWebsocketNetworkTimeoutMs;
    websocket.ping_interval_sec = kWebsocketPingIntervalSec;
    websocket.buffer_size = 2048;
    if (grant.worker_wss_url.rfind("wss://", 0) == 0) websocket.crt_bundle_attach = esp_crt_bundle_attach;
    esp_websocket_client_handle_t handle = esp_websocket_client_init(&websocket);
    if (handle == nullptr) {
        ESP_LOGE(kLogTag, "start failed: websocket client init");
        return false;
    }
    client_port_.reset(new (std::nothrow) wss::EspIdfWebsocketClientPort(handle));
    if (client_port_ == nullptr) {
        esp_websocket_client_destroy(handle);
        return false;
    }
    owner_.reset(new (std::nothrow) wss::WssOwner(
        *client_port_, wss::kMaximumCallbackEvents, wss::kMaximumQueuedCallbackBytes));
    if (owner_ == nullptr) {
        client_port_.reset();
        return false;
    }
    supervisor_work_signal_ = xSemaphoreCreateBinary();
    if (supervisor_work_signal_ == nullptr) {
        ESP_LOGE(kLogTag, "start failed: websocket event semaphore allocation");
        // The client has not started and no callback can be in flight. Keep this
        // OOM cleanup synchronous so it does not require another semaphore/task.
        if (!owner_->SupervisorClose(0)) {
            FailClosedRestart("websocket_partial_start_teardown_failed");
        }
        owner_.reset();
        client_port_.reset();
        return false;
    }
    owner_->BindCallbackReadyNotifier(NotifySupervisorWork, this);
    client_port_->BindEventSink(owner_.get());
    task_events_ = xEventGroupCreateWithCaps(MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (task_events_ == nullptr) {
        ESP_LOGE(kLogTag,
                 "start failed: event group allocation internal_free=%lu largest=%lu psram_free=%lu",
                 static_cast<unsigned long>(heap_caps_get_free_size(MALLOC_CAP_INTERNAL)),
                 static_cast<unsigned long>(heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL)),
                 static_cast<unsigned long>(heap_caps_get_free_size(MALLOC_CAP_SPIRAM)));
        // Owner::Start has not run yet, so synchronous destroy cannot race a
        // WebSocket callback and avoids allocating while already under OOM.
        if (!owner_->SupervisorClose(0)) {
            FailClosedRestart("websocket_partial_start_teardown_failed");
        }
        owner_->BindCallbackReadyNotifier(nullptr, nullptr);
        vSemaphoreDelete(supervisor_work_signal_);
        supervisor_work_signal_ = nullptr;
        owner_.reset();
        client_port_.reset();
        return false;
    }
    started_ = true;
    running_ = true;
    expected_task_bits_.store(0, std::memory_order_release);
    bool tasks_started = xTaskCreateWithCaps(
                             SupervisorTask, "rva-supervisor", 8192, this, 6,
                             &supervisor_task_, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT) == pdPASS;
    if (!tasks_started) ESP_LOGE(kLogTag, "start failed: supervisor task");
    if (tasks_started) {
        expected_task_bits_.fetch_or(kSupervisorStopped, std::memory_order_release);
    }
    ESP_LOGI(kLogTag, "control runtime ready: internal_free=%lu largest=%lu psram_free=%lu",
             static_cast<unsigned long>(heap_caps_get_free_size(MALLOC_CAP_INTERNAL)),
             static_cast<unsigned long>(heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL)),
             static_cast<unsigned long>(heap_caps_get_free_size(MALLOC_CAP_SPIRAM)));
    const bool owner_started = tasks_started && owner_->Start();
    websocket_started_.store(owner_started, std::memory_order_release);
    if (!tasks_started || !owner_started) {
        if (tasks_started && !owner_started) ESP_LOGE(kLogTag, "start failed: websocket owner start");
        ESP_LOGE(kLogTag, "start memory: internal_free=%lu largest=%lu psram_free=%lu",
                 static_cast<unsigned long>(heap_caps_get_free_size(MALLOC_CAP_INTERNAL)),
                 static_cast<unsigned long>(heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL)),
                 static_cast<unsigned long>(heap_caps_get_free_size(MALLOC_CAP_SPIRAM)));
        Stop();
        return false;
    }
    return true;
}

void VoiceRuntime::Stop() {
    if (!started_.exchange(false)) return;
    const bool was_running = running_.exchange(false);
    const bool websocket_was_started =
        websocket_started_.exchange(false, std::memory_order_acq_rel);
    if (supervisor_work_signal_ != nullptr) {
        xSemaphoreGive(supervisor_work_signal_);
    }
    if ((was_running || udp_refresh_requested_.load()) && session_opened_ && owner_ != nullptr) {
        protocol::SessionOpened identity;
        {
            std::lock_guard<std::mutex> lock(identity_mutex_);
            identity = opened_;
        }
        protocol::SessionClose close{
            .session = identity.session,
            .reason = "normal",
            .initiated_by = "device",
            .detail = udp_refresh_requested_.load() ? "udp_grant_refresh" : "",
        };
        std::string json;
        if (protocol::EncodeSessionClose(close, &json) == protocol::ControlError::kOk) {
            owner_->SendText(json, 250);
        }
    }
    if (owner_ != nullptr) owner_->RequestClose();
    if (udp_runtime_ != nullptr) udp_runtime_->RequestStop();
    if (task_events_ != nullptr) {
        // The supervisor is the sole creator of media tasks. Joining it first
        // freezes expected_task_bits_ and prevents task creation from racing
        // queue/codec/pipeline teardown below.
        const EventBits_t supervisor_bits =
            expected_task_bits_.load(std::memory_order_acquire) & kSupervisorStopped;
        if (supervisor_bits != 0) {
            const EventBits_t stopped = xEventGroupWaitBits(
                task_events_, supervisor_bits, pdFALSE, pdTRUE, pdMS_TO_TICKS(5000));
            if ((stopped & supervisor_bits) != supervisor_bits) {
                FailClosedRestart("supervisor_join_timeout");
            }
        }
        const EventBits_t media_bits =
            expected_task_bits_.load(std::memory_order_acquire) & ~kSupervisorStopped;
        if (media_bits != 0) {
            const EventBits_t stopped = xEventGroupWaitBits(
                task_events_, media_bits, pdFALSE, pdTRUE, pdMS_TO_TICKS(5000));
            if ((stopped & media_bits) != media_bits) {
                // Tasks own codec/driver state while running, so forced deletion
                // is unsafe. Fail closed while all shared resources remain alive.
                FailClosedRestart("media_join_timeout");
            }
        }
    }
    if (owner_ != nullptr) {
        const bool websocket_closed = websocket_was_started
                                          ? CloseWebsocketBounded(kWebsocketTeardownTimeoutMs)
                                          : owner_->SupervisorClose(0);
        if (!websocket_closed) {
            FailClosedRestart("websocket_teardown_failed");
        }
    }
    if (owner_ != nullptr) {
        owner_->BindCallbackReadyNotifier(nullptr, nullptr);
    }
    if (supervisor_work_signal_ != nullptr) {
        vSemaphoreDelete(supervisor_work_signal_);
        supervisor_work_signal_ = nullptr;
    }
    if (udp_runtime_ != nullptr && !udp_runtime_->JoinAndClose(1000)) {
        events_.OnFailure("udp_join_timeout");
        FailClosedRestart("udp_join_timeout");
    }
    StopMediaRuntime();
    if (task_events_ != nullptr) {
        vEventGroupDeleteWithCaps(task_events_);
        task_events_ = nullptr;
    }
    owner_.reset();
    client_port_.reset();
    udp_runtime_.reset();
    udp_io_.reset();
    udp_session_.reset();
    udp_downlink_crypto_.reset();
    udp_uplink_crypto_.reset();
    session_.reset();
    supervisor_task_ = nullptr;
    capture_task_ = nullptr;
    uplink_framer_task_ = nullptr;
    uplink_encoder_task_ = nullptr;
    uplink_sender_task_ = nullptr;
    playback_task_ = nullptr;
    expected_task_bits_.store(0, std::memory_order_release);
    session_opened_ = false;
    session_open_deadline_us_ = 0;
    media_owner_ = voice::core::MediaOwner::kNone;
}

void VoiceRuntime::SupervisorTask(void* context) {
    auto* runtime = static_cast<VoiceRuntime*>(context);
    try {
        runtime->RunSupervisor();
    } catch (const std::bad_alloc&) {
        runtime->HandleTaskAllocationFailure("supervisor", kSupervisorStopped, true);
    } catch (...) {
        runtime->HandleTaskAllocationFailure("supervisor_exception", kSupervisorStopped, true);
    }
}

void VoiceRuntime::CaptureTask(void* context) {
    auto* runtime = static_cast<VoiceRuntime*>(context);
    try {
        runtime->RunCapture();
    } catch (const std::bad_alloc&) {
        runtime->HandleTaskAllocationFailure("capture", kCaptureStopped, true);
    } catch (...) {
        runtime->HandleTaskAllocationFailure("capture_exception", kCaptureStopped, true);
    }
}

void VoiceRuntime::UplinkFramerTask(void* context) {
    auto* runtime = static_cast<VoiceRuntime*>(context);
    try {
        runtime->RunUplinkFramer();
    } catch (const std::bad_alloc&) {
        runtime->HandleTaskAllocationFailure("uplink_framer", kUplinkFramerStopped, true);
    } catch (...) {
        runtime->HandleTaskAllocationFailure("uplink_framer_exception", kUplinkFramerStopped, true);
    }
}

void VoiceRuntime::UplinkEncoderTask(void* context) {
    auto* runtime = static_cast<VoiceRuntime*>(context);
    try {
        runtime->RunUplinkEncoder();
    } catch (const std::bad_alloc&) {
        runtime->HandleTaskAllocationFailure("uplink_encoder", kUplinkEncoderStopped, true);
    } catch (...) {
        runtime->HandleTaskAllocationFailure("uplink_encoder_exception", kUplinkEncoderStopped, true);
    }
}

void VoiceRuntime::UplinkSenderTask(void* context) {
    auto* runtime = static_cast<VoiceRuntime*>(context);
    try {
        runtime->RunUplinkSender();
    } catch (const std::bad_alloc&) {
        runtime->HandleTaskAllocationFailure("uplink_sender", kUplinkSenderStopped, true);
    } catch (...) {
        runtime->HandleTaskAllocationFailure("uplink_sender_exception", kUplinkSenderStopped, true);
    }
}

void VoiceRuntime::PlaybackTask(void* context) {
    auto* runtime = static_cast<VoiceRuntime*>(context);
    try {
        runtime->RunPlayback();
    } catch (const std::bad_alloc&) {
        runtime->HandleTaskAllocationFailure("playback", kPlaybackStopped, true);
    } catch (...) {
        runtime->HandleTaskAllocationFailure("playback_exception", kPlaybackStopped, true);
    }
}

void VoiceRuntime::WebsocketTeardownTask(void* context) {
    auto* teardown = static_cast<WebsocketTeardownContext*>(context);
    try {
        teardown->result = teardown->owner != nullptr &&
                           teardown->owner->SupervisorClose(kWebsocketCloseTimeoutMs);
    } catch (...) {
        teardown->result = false;
    }
    xSemaphoreGive(teardown->done);
    // The owner task performs the caps-aware deletion after observing done,
    // so completion includes all teardown work and no task self-delete path
    // can allocate scheduler cleanup state.
    vTaskSuspend(nullptr);
}

void VoiceRuntime::NotifySupervisorWork(void* context) noexcept {
    auto* runtime = static_cast<VoiceRuntime*>(context);
    if (runtime != nullptr && runtime->supervisor_work_signal_ != nullptr) {
        xSemaphoreGive(runtime->supervisor_work_signal_);
    }
}

bool VoiceRuntime::CloseWebsocketBounded(uint32_t timeout_ms) {
    if (owner_ == nullptr) return true;
    SemaphoreHandle_t done = xSemaphoreCreateBinary();
    if (done == nullptr) {
        ESP_LOGE(kLogTag, "websocket teardown failed: semaphore allocation");
        return false;
    }
    WebsocketTeardownContext teardown{
        .owner = owner_.get(),
        .done = done,
        .result = false,
    };
    TaskHandle_t task = nullptr;
    const BaseType_t created = xTaskCreateWithCaps(
        WebsocketTeardownTask,
        "rva-wss-close",
        kWebsocketTeardownStackBytes,
        &teardown,
        7,
        &task,
        MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (created != pdPASS) {
        vSemaphoreDelete(done);
        ESP_LOGE(kLogTag, "websocket teardown failed: task allocation");
        return false;
    }
    if (xSemaphoreTake(done, pdMS_TO_TICKS(timeout_ms)) != pdTRUE) {
        // The task owns a pointer into this runtime and may still be inside the
        // pinned SDK. Returning would create a UAF; restart while the stack
        // context and owner are still alive.
        FailClosedRestart("websocket_teardown_timeout");
    }
    const bool result = teardown.result;
    vTaskDeleteWithCaps(task);
    vSemaphoreDelete(done);
    return result;
}

[[noreturn]] void VoiceRuntime::FailClosedRestart(const char* category) noexcept {
    ESP_LOGE(kLogTag, "runtime cannot recover in-process: %s",
             category == nullptr ? "unknown" : category);
    try {
        events_.OnFailure(category);
    } catch (...) {
        // Failure reporting must not delay the controlled restart.
    }
    if (fail_closed_hook_ != nullptr) {
        fail_closed_hook_(fail_closed_context_);
    }
    vTaskDelay(pdMS_TO_TICKS(50));
    esp_restart();
    std::abort();
}

[[noreturn]] void VoiceRuntime::HandleTaskAllocationFailure(
    const char* task_name, EventBits_t stopped_bit, bool stack_uses_caps) noexcept {
    ESP_LOGE(kLogTag, "%s task allocation failed", task_name);
    running_ = false;
    try {
        events_.OnFailure("task_allocation");
    } catch (...) {
        // Failure reporting must not escape a FreeRTOS task entry.
    }
    MarkTaskStopped(stopped_bit);
    if (stack_uses_caps) {
        vTaskDeleteWithCaps(nullptr);
    } else {
        vTaskDelete(nullptr);
    }
    std::abort();
}

void VoiceRuntime::RunSupervisor() {
    uint32_t observed_dropped_events = 0;
    uint32_t observed_udp_handoff_dropped = 0;
    uint32_t observed_udp_jitter_dropped = 0;
    uint32_t observed_udp_media_age_dropped = 0;
    uint32_t observed_wss_queue_dropped = 0;
    int64_t next_uplink_metrics_us = esp_timer_get_time() + kUplinkMetricsIntervalUs;
    while (running_) {
        if (!DrainPlaybackFacts()) {
            events_.OnFailure("playback_fact_send");
            running_ = false;
            continue;
        }
        const uint32_t dropped_events = owner_->dropped_events();
        if (dropped_events != observed_dropped_events) {
            observed_dropped_events = dropped_events;
            events_.OnFailure("websocket_callback_overflow");
            running_ = false;
            continue;
        }
        const int64_t now_us = esp_timer_get_time();
        const int64_t session_open_deadline_us = session_open_deadline_us_.load();
        if (!session_opened_.load() && session_open_deadline_us > 0 &&
            now_us >= session_open_deadline_us) {
            events_.OnFailure("session_open_timeout");
            running_ = false;
            continue;
        }
        if (media_started_.load() && now_us >= next_uplink_metrics_us) {
            LogAndResetUplinkMetrics();
            next_uplink_metrics_us = now_us + kUplinkMetricsIntervalUs;
        }
        if (media_owner_ == voice::core::MediaOwner::kUdp && udp_runtime_ != nullptr) {
            const int64_t refresh_deadline_us = udp_refresh_deadline_us_.load();
            const int64_t liveness_timeout_us = udp_liveness_timeout_us_.load();
            const int64_t last_receive_us = udp_runtime_->last_authenticated_receive_us();
            const uint32_t handoff_dropped = udp_runtime_->playout_queue_dropped();
            const uint32_t jitter_dropped = udp_runtime_->stats().queue_dropped;
            const uint32_t media_age_dropped = udp_runtime_->playout_media_age_dropped();
            const char* failure = nullptr;
            if (refresh_deadline_us > 0 && now_us >= refresh_deadline_us) {
                ESP_LOGI(kLogTag, "UDP monotonic refresh deadline reached; requesting fresh session");
                udp_refresh_requested_ = true;
                running_ = false;
                continue;
            } else if (liveness_timeout_us > 0 && last_receive_us > 0 &&
                       now_us - last_receive_us >= liveness_timeout_us) {
                failure = "udp_media_inactive";
            } else if (handoff_dropped != observed_udp_handoff_dropped ||
                       jitter_dropped != observed_udp_jitter_dropped) {
                observed_udp_handoff_dropped = handoff_dropped;
                observed_udp_jitter_dropped = jitter_dropped;
                ESP_LOGW(kLogTag, "udp playout dropped handoff=%lu jitter=%lu",
                         static_cast<unsigned long>(handoff_dropped),
                         static_cast<unsigned long>(jitter_dropped));
            } else if (media_age_dropped != observed_udp_media_age_dropped) {
                observed_udp_media_age_dropped = media_age_dropped;
                ESP_LOGW(kLogTag, "udp stale media dropped=%lu",
                         static_cast<unsigned long>(media_age_dropped));
            } else if (udp_next_keepalive_us_.load() > 0 &&
                       now_us >= udp_next_keepalive_us_.load()) {
                if (!udp_runtime_->SendKeepalive()) {
                    failure = "udp_keepalive_send";
                } else {
                    udp_next_keepalive_us_ = now_us + udp_heartbeat_interval_us_.load();
                }
            }
            if (failure != nullptr) {
                events_.OnFailure(failure);
                fallback_to_wss_ = true;
                udp_runtime_->RequestStop();
                running_ = false;
                continue;
            }
        }
        const uint32_t wss_queue_dropped = wss_playback_queue_dropped_.load();
        if (wss_queue_dropped != observed_wss_queue_dropped) {
            observed_wss_queue_dropped = wss_queue_dropped;
            ESP_LOGW(kLogTag, "wss playback queue dropped=%lu",
                     static_cast<unsigned long>(wss_queue_dropped));
        }
        wss::OwnedClientEvent event;
        if (!owner_->Poll(&event)) {
            if (supervisor_work_signal_ != nullptr) {
                xSemaphoreTake(supervisor_work_signal_, pdMS_TO_TICKS(100));
            }
            continue;
        }
        if (event.type == wss::ClientEventType::kConnected) {
            events_.OnConnection(true);
            if (!SendSessionOpen()) {
                events_.OnFailure("session_open_send");
                running_ = false;
            } else {
                session_open_deadline_us_ = esp_timer_get_time() + kSessionOpenDeadlineUs;
            }
            continue;
        }
        if (event.type == wss::ClientEventType::kDisconnected || event.type == wss::ClientEventType::kError) {
            events_.OnConnection(false);
            running_ = false;
            continue;
        }
        std::vector<uint8_t> frame;
        const wss::AssembleResult assembled = assembler_.Consume(event, &frame);
        if (assembled == wss::AssembleResult::kRejected) {
            events_.OnFailure("websocket_frame");
            running_ = false;
        } else if (assembled == wss::AssembleResult::kComplete) {
            if (event.type == wss::ClientEventType::kTextFragment) HandleControl(frame);
            if (event.type == wss::ClientEventType::kBinaryFragment) HandleMedia(frame);
        }
    }
    if (!DrainPlaybackFacts()) {
        events_.OnFailure("playback_fact_send");
    }
    if (media_started_.load()) LogAndResetUplinkMetrics();
    ESP_LOGI(kLogTag, "supervisor task minimum free stack: %lu bytes",
             static_cast<unsigned long>(uxTaskGetStackHighWaterMark(nullptr)));
    MarkTaskStopped(kSupervisorStopped);
    vTaskDeleteWithCaps(nullptr);
}

void VoiceRuntime::HandleControl(const std::vector<uint8_t>& frame) {
    protocol::ServerMessage message;
    if (protocol::ParseServerMessage(frame.data(), frame.size(), &message) != protocol::ControlError::kOk) {
        events_.OnFailure("control_parse");
        running_ = false;
        return;
    }
    if (const auto* opened = std::get_if<protocol::SessionOpened>(&message)) {
        const auto core_profile = voice::contracts::ParseTransportProfile(opened->selected_media_profile);
        const voice::core::MediaOwner selected_owner =
            opened->selected_media_profile == "udp-opus-gcm/1"
                ? voice::core::MediaOwner::kUdp
                : voice::core::MediaOwner::kWss;
        if (opened->session.session_epoch != expected_session_epoch_ ||
            session_->Accept(message) != wss::AdmissionResult::kAccepted || !core_profile ||
            (selected_owner == voice::core::MediaOwner::kUdp && !ConfigureUdp(*opened)) ||
            !core_gate_.CommitMedia(*core_profile, selected_owner)) {
            if (selected_owner == voice::core::MediaOwner::kUdp) fallback_to_wss_ = true;
            events_.OnFailure("session_opened");
            running_ = false;
            return;
        }
        {
            std::lock_guard<std::mutex> lock(identity_mutex_);
            opened_ = *opened;
        }
        media_owner_ = selected_owner;
        events_.OnMediaProfile(
            selected_owner == voice::core::MediaOwner::kUdp ? MediaPreference::kUdp
                                                             : MediaPreference::kWss);
        session_opened_ = true;
        session_open_deadline_us_ = 0;
        if (!StartMediaRuntime()) {
            events_.OnFailure("media_runtime_start");
            running_ = false;
            return;
        }
        if (selected_owner == voice::core::MediaOwner::kUdp) {
            const int64_t heartbeat_us = static_cast<int64_t>(opened->heartbeat_interval_ms) * 1000;
            const int64_t idle_us = static_cast<int64_t>(opened->idle_timeout_ms) * 1000;
            if (heartbeat_us <= 0 || idle_us < heartbeat_us) {
                events_.OnFailure("udp_heartbeat_contract");
                fallback_to_wss_ = true;
                running_ = false;
                return;
            }
            udp_heartbeat_interval_us_ = heartbeat_us;
            udp_liveness_timeout_us_ = idle_us;
            udp_next_keepalive_us_ = esp_timer_get_time() + heartbeat_us;
        }
        return;
    }
    const wss::AdmissionResult admitted = session_->Accept(message);
    if (admitted != wss::AdmissionResult::kAccepted) {
        events_.OnFailure("control_fence");
        running_ = false;
        return;
    }
    if (const auto* error = std::get_if<protocol::SessionError>(&message)) {
        events_.OnFailure(error->retryable ? "session_error_retryable" : "session_error_terminal");
        running_ = false;
    } else if (const auto* transcript = std::get_if<protocol::Transcript>(&message)) {
        events_.OnTranscript(transcript->text.c_str(), transcript->final);
        if (transcript->final && !playback_enabled_.load()) {
            events_.OnConversationPhase(ConversationPhase::kThinking);
        }
    } else if (const auto* response = std::get_if<protocol::ResponseEvent>(&message)) {
        const protocol::ResponseTarget target{
            response->response_id,
            response->generation,
        };
        if (response->type == protocol::ServerMessageType::kResponseBegin) {
            if (!core_gate_.AdvancePlaybackGeneration(response->generation) ||
                !EnqueuePlaybackCommand(PlaybackCommandType::kBegin, target, 0)) {
                events_.OnFailure("playback_generation");
                running_ = false;
            } else {
                events_.OnConversationPhase(ConversationPhase::kSpeaking);
            }
        } else if (response->type == protocol::ServerMessageType::kResponseEnd &&
                   response->outcome == protocol::ResponseOutcome::kCompleted) {
            if (!response->final_media_sequence.has_value() ||
                !EnqueuePlaybackCommand(
                    PlaybackCommandType::kComplete,
                    target,
                    *response->final_media_sequence)) {
                events_.OnFailure("playback_completion");
                running_ = false;
            }
        }
        if (response->type == protocol::ServerMessageType::kResponseText) {
            events_.OnResponseText(response->text.c_str());
        }
    } else if (const auto* stop = std::get_if<protocol::PlaybackStop>(&message)) {
        if (!EnqueuePlaybackCommand(
                PlaybackCommandType::kStop,
                stop->target,
                stop->fence_generation)) {
            events_.OnFailure("playback_stop_queue");
            running_ = false;
        }
    } else if (std::holds_alternative<protocol::SessionClose>(message)) {
        running_ = false;
    }
}

void VoiceRuntime::HandleMedia(const std::vector<uint8_t>& frame) {
    if (media_owner_ != voice::core::MediaOwner::kWss) {
        if (session_opened_) {
            events_.OnFailure("transport_mismatch");
            running_ = false;
        }
        return;
    }
    protocol::MediaHeader header;
    const wss::AdmissionResult admitted = session_->AcceptMedia(frame.data(), frame.size(), &header);
    if (admitted == wss::AdmissionResult::kStaleGeneration) {
        return;
    }
    if (admitted != wss::AdmissionResult::kAccepted || header.payload_length == 0) {
        ESP_LOGW(
            kLogTag,
            "WSS media rejected: admission=%u frame_bytes=%u",
            static_cast<unsigned>(admitted),
            static_cast<unsigned>(frame.size()));
        events_.OnFailure("wss_media_admission");
        running_ = false;
        return;
    }
    MediaPacket packet;
    packet.size = static_cast<uint16_t>(header.payload_length);
    packet.sequence = header.sequence;
    packet.generation = header.generation;
    std::memcpy(packet.bytes.data(), frame.data() + protocol::kMediaHeaderBytes, packet.size);
    if (xQueueSend(playback_queue_, &packet, 0) != pdTRUE) {
        MediaPacket discarded;
        if (xQueueReceive(playback_queue_, &discarded, 0) == pdTRUE &&
            xQueueSend(playback_queue_, &packet, 0) == pdTRUE) {
            wss_playback_queue_dropped_.fetch_add(1);
        } else {
            wss_playback_queue_dropped_.fetch_add(1);
            ESP_LOGW(kLogTag, "wss playback queue saturated; dropping incoming media");
        }
    }
}

bool VoiceRuntime::EnqueuePlaybackCommand(
    PlaybackCommandType type,
    const protocol::ResponseTarget& target,
    uint32_t value) {
    if (playback_command_queue_ == nullptr || target.response_id.empty() ||
        target.response_id.size() > protocol::kMaxIdBytes || target.generation == 0) {
        return false;
    }
    PlaybackCommand command{
        .type = type,
        .generation = target.generation,
        .value = value,
    };
    std::memcpy(
        command.response_id.data(), target.response_id.data(), target.response_id.size());
    return xQueueSend(playback_command_queue_, &command, pdMS_TO_TICKS(20)) == pdTRUE;
}

bool VoiceRuntime::ProcessPlaybackCommands() {
    if (playback_command_queue_ == nullptr) return false;
    PlaybackCommand command;
    while (xQueueReceive(playback_command_queue_, &command, 0) == pdTRUE) {
        const protocol::ResponseTarget target{
            command.response_id.data(),
            command.generation,
        };
        std::optional<PlaybackFact> fact;
        if (command.type == PlaybackCommandType::kBegin) {
            if (!playback_state_.Begin(target) ||
                (udp_runtime_ != nullptr &&
                 !udp_runtime_->AdvanceGeneration(target.generation))) {
                return false;
            }
            playback_generation_ = target.generation;
            playback_enabled_ = true;
        } else if (command.type == PlaybackCommandType::kComplete) {
            if (!playback_state_.SetFinalMediaSequence(
                    target, command.value, esp_timer_get_time(), &fact)) {
                return false;
            }
        } else {
            if (!playback_state_.Stop(target, command.value, &fact)) return false;
            if (playback_queue_ != nullptr) xQueueReset(playback_queue_);
            if (udp_runtime_ != nullptr &&
                !udp_runtime_->FenceGeneration(command.value)) {
                return false;
            }
            playback_generation_ = command.value;
            playback_enabled_ = false;
        }
        if (fact.has_value()) {
            if (fact->type == PlaybackFactType::kEnded) {
                playback_enabled_ = false;
                events_.OnConversationPhase(ConversationPhase::kListening);
            }
            if (!PublishPlaybackFact(*fact)) return false;
        }
    }
    return true;
}

bool VoiceRuntime::PublishPlaybackFact(const PlaybackFact& fact) {
    if (playback_fact_queue_ == nullptr || fact.target.response_id.empty() ||
        fact.target.response_id.size() > protocol::kMaxIdBytes) {
        return false;
    }
    QueuedPlaybackFact queued{
        .type = fact.type,
        .generation = fact.target.generation,
        .outcome = fact.outcome,
        .played_samples = fact.played_samples,
    };
    std::memcpy(
        queued.response_id.data(), fact.target.response_id.data(),
        fact.target.response_id.size());
    if (fact.type == PlaybackFactType::kStarted) {
        queued.media_sequence = fact.first_media_sequence;
        queued.has_media_sequence = true;
    } else if (fact.last_media_sequence.has_value()) {
        queued.media_sequence = *fact.last_media_sequence;
        queued.has_media_sequence = true;
    }
    if (xQueueSend(playback_fact_queue_, &queued, 0) != pdTRUE) return false;
    if (supervisor_work_signal_ != nullptr) {
        xSemaphoreGive(supervisor_work_signal_);
    }
    return true;
}

bool VoiceRuntime::DrainPlaybackFacts() {
    if (playback_fact_queue_ == nullptr) return true;
    QueuedPlaybackFact queued;
    while (xQueueReceive(playback_fact_queue_, &queued, 0) == pdTRUE) {
        protocol::SessionIdentity session_identity;
        {
            std::lock_guard<std::mutex> lock(identity_mutex_);
            session_identity = opened_.session;
        }
        const protocol::ResponseTarget target{
            queued.response_id.data(),
            queued.generation,
        };
        std::string json;
        protocol::ControlError encoded = protocol::ControlError::kMissingOrInvalidField;
        if (queued.type == PlaybackFactType::kStarted) {
            encoded = protocol::EncodePlaybackStarted(
                {.session = session_identity,
                 .target = target,
                 .first_media_sequence = queued.media_sequence},
                &json);
        } else {
            protocol::PlaybackEnded ended{
                .session = session_identity,
                .target = target,
                .outcome = queued.outcome,
                .played_samples = queued.played_samples,
                .last_media_sequence = std::nullopt,
            };
            if (queued.has_media_sequence) {
                ended.last_media_sequence = queued.media_sequence;
            }
            encoded = protocol::EncodePlaybackEnded(ended, &json);
        }
        if (encoded != protocol::ControlError::kOk || owner_ == nullptr ||
            !owner_->SendText(json, 250)) {
            return false;
        }
    }
    return true;
}

bool VoiceRuntime::SendSessionOpen() {
    protocol::SessionOpen open;
    open.request_id = open_request_id_;
    open.device_id = device_id_;
    open.supported_media_profiles = {"wss-opus/1", "udp-opus-gcm/1"};
    open.preferred_media_profile =
        preferred_media_ == MediaPreference::kUdp ? "udp-opus-gcm/1" : "wss-opus/1";
    open.capabilities = {
        config_.aec,
        config_.vad,
        config_.wake_word,
        config_.display,
        config_.touch,
    };
    std::string json;
    return protocol::EncodeSessionOpen(open, &json) == protocol::ControlError::kOk && owner_->SendText(json, 1000);
}

bool VoiceRuntime::ConfigureUdp(const protocol::SessionOpened& opened) {
    if (!opened.udp_grant.has_value()) {
        ESP_LOGW(kLogTag, "udp configure failed: missing udp_grant");
        return false;
    }
    const protocol::UdpGrant& control = *opened.udp_grant;
    const auto now = std::chrono::system_clock::now();
    const auto now_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                            now.time_since_epoch())
                            .count();
    const bool clock_valid = now_ms >= 1577836800000LL;
    if (clock_valid && static_cast<uint64_t>(now_ms) >= control.expires_at_ms) {
        ESP_LOGW(kLogTag,
                 "udp configure failed: expired grant now_ms=%lld expires_at_ms=%llu",
                 static_cast<long long>(now_ms),
                 static_cast<unsigned long long>(control.expires_at_ms));
        return false;
    }
    const int64_t configured_us = esp_timer_get_time();
    const uint64_t maximum_refresh_ms =
        static_cast<uint64_t>((std::numeric_limits<int64_t>::max() - configured_us) / 1000);
    const uint64_t refresh_delay_ms = control.refresh_after_ms;
    const int64_t refresh_deadline_us = refresh_delay_ms > maximum_refresh_ms
                                            ? std::numeric_limits<int64_t>::max()
                                            : configured_us + static_cast<int64_t>(refresh_delay_ms) * 1000;
    if (!clock_valid) {
        ESP_LOGI(kLogTag,
                 "wall clock unavailable; UDP uses monotonic refresh_after_ms=%lu for local rotation; "
                 "server enforces expires_at_ms",
                 static_cast<unsigned long>(control.refresh_after_ms));
    }
    auto uplink = std::make_unique<udp::MbedTlsGcm>();
    auto downlink = std::make_unique<udp::MbedTlsGcm>();
    auto session = std::make_unique<udp::UdpSession>(*uplink, *downlink);
    auto io = std::make_unique<UdpSocketPort>();
    udp::SessionGrant grant;
    if (!io->Open(control.host, control.port, &grant.server)) {
        ESP_LOGW(kLogTag, "udp configure failed: socket open host=%s port=%u",
                 control.host.c_str(), static_cast<unsigned>(control.port));
        return false;
    }
    grant.media_id = opened.media_id;
    grant.media_epoch = opened.media_epoch;
    grant.initial_downlink_generation = playback_generation_;
    grant.uplink_key = control.uplink_key;
    grant.downlink_key = control.downlink_key;
    grant.uplink_salt = control.uplink_salt;
    grant.downlink_salt = control.downlink_salt;
    if (!session->Configure(grant)) {
        ESP_LOGW(kLogTag,
                 "udp configure failed: session grant rejected host=%s port=%u media_epoch=%llu generation=%lu",
                 control.host.c_str(), static_cast<unsigned>(control.port),
                 static_cast<unsigned long long>(opened.media_epoch),
                 static_cast<unsigned long>(playback_generation_));
        return false;
    }
    auto runtime = std::make_unique<udp::UdpRuntime>(*session, *io);
    if (!runtime->Start()) {
        ESP_LOGW(kLogTag, "udp configure failed: runtime start");
        return false;
    }
    const int64_t started_us = esp_timer_get_time();
    const int64_t deadline_us = started_us +
                                static_cast<int64_t>(control.probe_timeout_ms) * 1000;
    int64_t next_probe_us = started_us;
    uint32_t probe_attempts = 0;
    uint32_t probe_send_failures = 0;
    while (running_ && !session->ready() && esp_timer_get_time() < deadline_us) {
        const int64_t now_us = esp_timer_get_time();
        if (now_us >= next_probe_us) {
            if (runtime->SendProbe()) {
                probe_attempts++;
            } else {
                probe_send_failures++;
            }
            next_probe_us = now_us + kUdpProbeRetryIntervalUs;
        }
        vTaskDelay(pdMS_TO_TICKS(10));
    }
    if (!session->ready()) {
        ESP_LOGW(kLogTag,
                 "udp configure failed: probe ack timeout host=%s port=%u timeout_ms=%u attempts=%lu send_failures=%lu",
                 control.host.c_str(), static_cast<unsigned>(control.port),
                 static_cast<unsigned>(control.probe_timeout_ms),
                 static_cast<unsigned long>(probe_attempts),
                 static_cast<unsigned long>(probe_send_failures));
        runtime->RequestStop();
        if (!runtime->JoinAndClose(1000)) {
            FailClosedRestart("udp_probe_teardown_failed");
        }
        return false;
    }
    ESP_LOGI(kLogTag, "udp probe acknowledged: attempts=%lu elapsed_ms=%lld",
             static_cast<unsigned long>(probe_attempts),
             static_cast<long long>((esp_timer_get_time() - started_us) / 1000));
    udp_uplink_crypto_ = std::move(uplink);
    udp_downlink_crypto_ = std::move(downlink);
    udp_session_ = std::move(session);
    udp_io_ = std::move(io);
    udp_runtime_ = std::move(runtime);
    // Anchor the refresh lease to UDP configuration, not to PROBE_ACK. A slow
    // probe must not silently extend the server-advertised session lifetime.
    udp_refresh_deadline_us_ = refresh_deadline_us;
    return true;
}

bool VoiceRuntime::StartMediaRuntime() {
    static_assert(std::is_trivially_copyable_v<EncodedUplinkFrame>);
    if (media_started_.load()) return true;
    if (task_events_ == nullptr || !running_.load()) return false;
    uplink_framer_.Reset(0);
    uplink_sequence_ = 0;
    uplink_pcm_queue_dropped_ = 0;
    uplink_encoded_queue_dropped_ = 0;
    uplink_pcm_queue_high_water_ = 0;
    uplink_encoded_queue_high_water_ = 0;
    uplink_pcm_max_age_us_ = 0;
    uplink_encoded_max_age_us_ = 0;
    uplink_local_send_completion_max_age_us_ = 0;
    uplink_presend_stale_dropped_ = 0;
    wss_uplink_send_failures_ = 0;
    const auto reset_stage = [](StageCounters* counters) {
        counters->count = 0;
        counters->total_us = 0;
        counters->max_us = 0;
        counters->deadline_misses = 0;
    };
    reset_stage(&capture_stage_);
    reset_stage(&framing_stage_);
    reset_stage(&encode_stage_);
    reset_stage(&send_stage_);
    playback_queue_ = xQueueCreateWithCaps(
        6, sizeof(MediaPacket), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    playback_command_queue_ = xQueueCreateWithCaps(
        8, sizeof(PlaybackCommand), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    playback_fact_queue_ = xQueueCreateWithCaps(
        4, sizeof(QueuedPlaybackFact), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    uplink_pcm_queue_ = xQueueCreateWithCaps(
        kUplinkQueueCapacity, sizeof(UplinkPcmFrame), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    uplink_encoded_queue_ = xQueueCreateWithCaps(
        kUplinkQueueCapacity, sizeof(EncodedUplinkFrame), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (playback_queue_ == nullptr || playback_command_queue_ == nullptr ||
        playback_fact_queue_ == nullptr || uplink_pcm_queue_ == nullptr ||
        uplink_encoded_queue_ == nullptr || !codec_.Start() ||
        !pipeline_.Start().ok() || !StartPlaybackResampler()) {
        ESP_LOGE(kLogTag,
                 "media start failed: queues=%d/%d/%d/%d/%d internal_free=%lu largest=%lu psram_free=%lu",
                 playback_queue_ != nullptr, playback_command_queue_ != nullptr,
                 playback_fact_queue_ != nullptr, uplink_pcm_queue_ != nullptr,
                 uplink_encoded_queue_ != nullptr,
                 static_cast<unsigned long>(heap_caps_get_free_size(MALLOC_CAP_INTERNAL)),
                 static_cast<unsigned long>(heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL)),
                 static_cast<unsigned long>(heap_caps_get_free_size(MALLOC_CAP_SPIRAM)));
        return false;
    }
    EventBits_t started_media_bits = 0;
    bool tasks_started = running_.load();
    if (tasks_started) {
        tasks_started = xTaskCreatePinnedToCoreWithCaps(
                            CaptureTask, "rva-capture", kCaptureTaskStackBytes, this, 7,
                            &capture_task_, CaptureTaskCore(),
                            MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT) == pdPASS;
        if (!tasks_started) ESP_LOGE(kLogTag, "media start failed: capture task");
    }
    if (tasks_started) {
        expected_task_bits_.fetch_or(kCaptureStopped, std::memory_order_release);
        started_media_bits |= kCaptureStopped;
    }
    if (tasks_started && !running_.load()) tasks_started = false;
    if (tasks_started) {
        tasks_started = xTaskCreatePinnedToCoreWithCaps(
                            UplinkFramerTask, "rva-uplink-frame", kUplinkFramerTaskStackBytes,
                            this, 7, &uplink_framer_task_, FramerTaskCore(),
                            MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT) == pdPASS;
        if (!tasks_started) ESP_LOGE(kLogTag, "media start failed: uplink framer task");
        if (tasks_started) {
            expected_task_bits_.fetch_or(kUplinkFramerStopped, std::memory_order_release);
            started_media_bits |= kUplinkFramerStopped;
        }
    }
    if (tasks_started && !running_.load()) tasks_started = false;
    if (tasks_started) {
        tasks_started = xTaskCreatePinnedToCoreWithCaps(
                            UplinkEncoderTask, "rva-uplink-encode", kUplinkEncoderTaskStackBytes,
                            this, 6, &uplink_encoder_task_, EncoderTaskCore(),
                            MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT) == pdPASS;
        if (!tasks_started) ESP_LOGE(kLogTag, "media start failed: uplink encoder task");
        if (tasks_started) {
            expected_task_bits_.fetch_or(kUplinkEncoderStopped, std::memory_order_release);
            started_media_bits |= kUplinkEncoderStopped;
        }
    }
    if (tasks_started && !running_.load()) tasks_started = false;
    if (tasks_started) {
        tasks_started = xTaskCreatePinnedToCoreWithCaps(
                            UplinkSenderTask, "rva-uplink-send", kUplinkSenderTaskStackBytes,
                            this, 5, &uplink_sender_task_, SenderTaskCore(),
                            MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT) == pdPASS;
        if (!tasks_started) ESP_LOGE(kLogTag, "media start failed: uplink sender task");
        if (tasks_started) {
            expected_task_bits_.fetch_or(kUplinkSenderStopped, std::memory_order_release);
            started_media_bits |= kUplinkSenderStopped;
        }
    }
    if (tasks_started && !running_.load()) tasks_started = false;
    if (tasks_started) {
        tasks_started = xTaskCreateWithCaps(
                            PlaybackTask, "rva-playback", kPlaybackTaskStackBytes, this, 6,
                            &playback_task_, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT) == pdPASS;
        if (!tasks_started) ESP_LOGE(kLogTag, "media start failed: playback task");
        if (tasks_started) {
            expected_task_bits_.fetch_or(kPlaybackStopped, std::memory_order_release);
            started_media_bits |= kPlaybackStopped;
        }
    }
    if (tasks_started && !running_.load()) tasks_started = false;
    if (!tasks_started) {
        ESP_LOGW(kLogTag,
                 "media start incomplete: bits=0x%lx running=%d internal_free=%lu largest=%lu psram_free=%lu",
                 static_cast<unsigned long>(started_media_bits), running_.load(),
                 static_cast<unsigned long>(heap_caps_get_free_size(MALLOC_CAP_INTERNAL)),
                 static_cast<unsigned long>(heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL)),
                 static_cast<unsigned long>(heap_caps_get_free_size(MALLOC_CAP_SPIRAM)));
        running_ = false;
        // Stop is the sole join/teardown owner. It first waits for this
        // supervisor to stop, then joins the stable started_media_bits mask.
        return false;
    }
    ESP_LOGI(kLogTag,
             "media runtime ready: affinity=%s internal_free=%lu largest=%lu psram_free=%lu",
#if CONFIG_RVA_UPLINK_AFFINITY_AUDIO_CPU1
             "audio_cpu1",
#elif CONFIG_RVA_UPLINK_AFFINITY_OPUS_CPU1
             "opus_cpu1_sender_cpu0",
#else
             "unpinned",
#endif
             static_cast<unsigned long>(heap_caps_get_free_size(MALLOC_CAP_INTERNAL)),
             static_cast<unsigned long>(heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL)),
             static_cast<unsigned long>(heap_caps_get_free_size(MALLOC_CAP_SPIRAM)));
    media_started_ = true;
    return true;
}

void VoiceRuntime::StopMediaRuntime() {
    media_started_ = false;
    StopPlaybackResampler();
    pipeline_.Stop();
    codec_.Stop();
    if (playback_queue_ != nullptr) {
        vQueueDeleteWithCaps(playback_queue_);
        playback_queue_ = nullptr;
    }
    if (playback_command_queue_ != nullptr) {
        vQueueDeleteWithCaps(playback_command_queue_);
        playback_command_queue_ = nullptr;
    }
    if (playback_fact_queue_ != nullptr) {
        vQueueDeleteWithCaps(playback_fact_queue_);
        playback_fact_queue_ = nullptr;
    }
    if (uplink_pcm_queue_ != nullptr) {
        vQueueDeleteWithCaps(uplink_pcm_queue_);
        uplink_pcm_queue_ = nullptr;
    }
    if (uplink_encoded_queue_ != nullptr) {
        vQueueDeleteWithCaps(uplink_encoded_queue_);
        uplink_encoded_queue_ = nullptr;
    }
}

void VoiceRuntime::RunCapture() {
    const size_t samples_per_channel = frontend_.feed_samples_per_channel();
    const size_t payload_samples = samples_per_channel * 2;
    std::vector<int16_t> buffer(payload_samples);
    uint32_t consecutive_failures = 0;
    while (running_) {
        audio::MutablePcmView capture{
            .samples = buffer.data(),
            .capacity_samples = payload_samples,
        };
        const audio::PortResult captured = pipeline_.ReadCapture(&capture, 100);
        if (captured == audio::PortResult::kOk) {
            const int64_t started_us = esp_timer_get_time();
            const audio::PortResult fed = pipeline_.FeedFrontend(
                {capture.samples, capture.sample_count, capture.sample_rate_hz, capture.channel_count});
            RecordStage(
                &capture_stage_,
                static_cast<uint32_t>(std::max<int64_t>(0, esp_timer_get_time() - started_us)),
                kCaptureProcessingDeadlineUs);
            consecutive_failures = fed == audio::PortResult::kOk ? 0 : consecutive_failures + 1;
            // I2S capture is the real-time pacer. A fixed tick delay here would
            // discard input because one FreeRTOS tick is 10 ms on this target.
        } else {
            consecutive_failures++;
        }
        if (consecutive_failures >= kAudioFailureLimit) {
            events_.OnFailure("capture_pipeline_failure");
            running_ = false;
        }
    }
    MarkTaskStopped(kCaptureStopped);
    vTaskDeleteWithCaps(nullptr);
}

void VoiceRuntime::RunUplinkFramer() {
    std::vector<int16_t> fetched(frontend_.fetch_samples_per_channel());
    uint32_t consecutive_failures = 0;
    bool observed_vad = frontend_.speech_active();
    ESP_LOGI(kLogTag, "uplink audio state: afe_vad=%s",
             observed_vad ? "speech" : "noise");
    while (running_) {
        audio::MutablePcmView output{.samples = fetched.data(), .capacity_samples = fetched.size()};
        const audio::PortResult fetched_result = pipeline_.FetchFrontend(&output, 100);
        if (fetched_result != audio::PortResult::kOk) {
            if (++consecutive_failures >= kAudioFailureLimit) {
                events_.OnFailure("frontend_fetch_failure");
                running_ = false;
            }
            continue;
        }
        consecutive_failures = 0;
        const bool current_vad = frontend_.speech_active();
        if (current_vad != observed_vad) {
            observed_vad = current_vad;
            ESP_LOGI(kLogTag, "uplink audio state: afe_vad=%s",
                     observed_vad ? "speech" : "noise");
        }
        const int64_t captured_at_us = esp_timer_get_time();
        const int64_t framing_started_us = captured_at_us;
        uplink_framer_.Consume(
            output.samples, output.sample_count, captured_at_us,
            [this](const UplinkPcmFrame& frame) {
                const LatestEnqueueResult queued = EnqueueLatest(
                    frame,
                    [this](const UplinkPcmFrame& value) {
                        return xQueueSend(uplink_pcm_queue_, &value, 0) == pdTRUE;
                    },
                    [this](UplinkPcmFrame* value) {
                        return xQueueReceive(uplink_pcm_queue_, value, 0) == pdTRUE;
                    });
                if (queued == LatestEnqueueResult::kReplacedOldest) {
                    ++uplink_pcm_queue_dropped_;
                } else if (queued == LatestEnqueueResult::kFailed) {
                    events_.OnFailure("uplink_pcm_queue");
                    running_ = false;
                }
                UpdateQueueHighWater(&uplink_pcm_queue_high_water_, uplink_pcm_queue_);
            });
        RecordStage(
            &framing_stage_,
            static_cast<uint32_t>(std::max<int64_t>(0, esp_timer_get_time() - framing_started_us)),
            kFramingProcessingDeadlineUs);
    }
    ESP_LOGI(kLogTag, "uplink framer task minimum free stack: %lu bytes",
             static_cast<unsigned long>(uxTaskGetStackHighWaterMark(nullptr)));
    MarkTaskStopped(kUplinkFramerStopped);
    vTaskDeleteWithCaps(nullptr);
}

void VoiceRuntime::RunUplinkEncoder() {
    UplinkPcmFrame pcm;
    uint32_t consecutive_failures = 0;
    uint32_t telemetry_packets = 0;
    uint32_t telemetry_dtx_packets = 0;
    uint32_t telemetry_opus_bytes = 0;
    size_t telemetry_min_opus_bytes = std::numeric_limits<size_t>::max();
    size_t telemetry_max_opus_bytes = 0;
    while (running_) {
        if (xQueueReceive(uplink_pcm_queue_, &pcm, pdMS_TO_TICKS(100)) != pdTRUE) continue;
        const int64_t started_us = esp_timer_get_time();
        UpdateMaximum(
            &uplink_pcm_max_age_us_,
            static_cast<uint32_t>(std::max<int64_t>(0, started_us - pcm.captured_at_us)));
        EncodedUplinkFrame encoded;
        size_t opus_size = 0;
        const bool succeeded = codec_.Encode60Ms(
            pcm.samples.data(), pcm.samples.size(), encoded.bytes.data(), encoded.bytes.size(), &opus_size);
        RecordStage(
            &encode_stage_,
            static_cast<uint32_t>(std::max<int64_t>(0, esp_timer_get_time() - started_us)),
            kEncodeDeadlineUs);
        if (!succeeded || opus_size == 0 || opus_size > encoded.bytes.size()) {
            events_.OnFailure(opus_size == 0 && succeeded ? "opus_empty_frame" : "opus_encode");
            if (++consecutive_failures >= kAudioFailureLimit || opus_size == 0) running_ = false;
            continue;
        }
        consecutive_failures = 0;
        encoded.size = static_cast<uint16_t>(opus_size);
        encoded.timestamp = pcm.timestamp;
        encoded.captured_at_us = pcm.captured_at_us;
        telemetry_packets++;
        telemetry_opus_bytes += static_cast<uint32_t>(opus_size);
        telemetry_min_opus_bytes = std::min(telemetry_min_opus_bytes, opus_size);
        telemetry_max_opus_bytes = std::max(telemetry_max_opus_bytes, opus_size);
        if (opus_size <= 64) telemetry_dtx_packets++;
        if (telemetry_packets == 1000) {
            ESP_LOGI(
                kLogTag,
                "uplink audio window: packets=%lu opus_bytes_avg=%lu min=%u max=%u dtx=%lu",
                static_cast<unsigned long>(telemetry_packets),
                static_cast<unsigned long>(telemetry_opus_bytes / telemetry_packets),
                static_cast<unsigned>(telemetry_min_opus_bytes),
                static_cast<unsigned>(telemetry_max_opus_bytes),
                static_cast<unsigned long>(telemetry_dtx_packets));
            telemetry_packets = 0;
            telemetry_dtx_packets = 0;
            telemetry_opus_bytes = 0;
            telemetry_min_opus_bytes = std::numeric_limits<size_t>::max();
            telemetry_max_opus_bytes = 0;
        }
        const LatestEnqueueResult queued = EnqueueLatest(
            encoded,
            [this](const EncodedUplinkFrame& value) {
                return xQueueSend(uplink_encoded_queue_, &value, 0) == pdTRUE;
            },
            [this](EncodedUplinkFrame* value) {
                return xQueueReceive(uplink_encoded_queue_, value, 0) == pdTRUE;
            });
        if (queued == LatestEnqueueResult::kReplacedOldest) {
            ++uplink_encoded_queue_dropped_;
        } else if (queued == LatestEnqueueResult::kFailed) {
            events_.OnFailure("uplink_encoded_queue");
            running_ = false;
        }
        UpdateQueueHighWater(&uplink_encoded_queue_high_water_, uplink_encoded_queue_);
    }
    ESP_LOGI(kLogTag, "uplink encoder task minimum free stack: %lu bytes",
             static_cast<unsigned long>(uxTaskGetStackHighWaterMark(nullptr)));
    MarkTaskStopped(kUplinkEncoderStopped);
    vTaskDeleteWithCaps(nullptr);
}

void VoiceRuntime::RunUplinkSender() {
    EncodedUplinkFrame encoded;
    while (running_) {
        if (xQueueReceive(uplink_encoded_queue_, &encoded, pdMS_TO_TICKS(100)) != pdTRUE) continue;
        const int64_t started_us = esp_timer_get_time();
        UpdateMaximum(
            &uplink_encoded_max_age_us_,
            static_cast<uint32_t>(std::max<int64_t>(0, started_us - encoded.captured_at_us)));
        if (media_owner_ == voice::core::MediaOwner::kUdp) {
            if (udp_runtime_ == nullptr ||
                (!udp_runtime_->SendAudio(encoded.bytes.data(), encoded.size, encoded.timestamp) &&
                 running_)) {
                events_.OnFailure("udp_uplink_send");
                fallback_to_wss_ = true;
                running_ = false;
            }
        } else if (media_owner_ == voice::core::MediaOwner::kWss) {
            if (!IsWssUplinkFrameFresh(encoded.captured_at_us, started_us)) {
                ++uplink_presend_stale_dropped_;
                continue;
            }
            protocol::SessionOpened identity;
            {
                std::lock_guard<std::mutex> lock(identity_mutex_);
                identity = opened_;
            }
            protocol::MediaHeader header;
            header.flags = 1;
            header.media_id = identity.media_id;
            header.media_epoch = identity.media_epoch;
            header.sequence = uplink_sequence_++;
            header.timestamp = encoded.timestamp;
            header.generation = 0;
            header.payload_length = encoded.size;
            std::array<uint8_t, protocol::kWssMaxFrameBytes> frame{};
            if (protocol::SerializeMediaHeader(
                    header, protocol::MediaDirection::kUplink, frame.data()) !=
                protocol::MediaError::kOk) {
                events_.OnFailure("uplink_header");
                running_ = false;
            } else {
                std::memcpy(
                    frame.data() + protocol::kMediaHeaderBytes,
                    encoded.bytes.data(), encoded.size);
                if (!owner_->SendMedia(
                        frame.data(), protocol::kMediaHeaderBytes + encoded.size,
                        kWssMediaSendTimeoutMs) && running_) {
                    ++wss_uplink_send_failures_;
                    events_.OnFailure("uplink_send");
                    running_ = false;
                }
            }
        } else if (session_opened_) {
            events_.OnFailure("uplink_send");
            running_ = false;
        }
        const int64_t completed_us = esp_timer_get_time();
        UpdateMaximum(
            &uplink_local_send_completion_max_age_us_,
            static_cast<uint32_t>(std::max<int64_t>(0, completed_us - encoded.captured_at_us)));
        RecordStage(
            &send_stage_,
            static_cast<uint32_t>(std::max<int64_t>(0, completed_us - started_us)),
            kSendDeadlineUs);
    }
    ESP_LOGI(kLogTag, "uplink sender task minimum free stack: %lu bytes",
             static_cast<unsigned long>(uxTaskGetStackHighWaterMark(nullptr)));
    MarkTaskStopped(kUplinkSenderStopped);
    vTaskDeleteWithCaps(nullptr);
}

void VoiceRuntime::RunPlayback() {
    MediaPacket packet;
    uint32_t observed_wss_dropped = wss_playback_queue_dropped_.load();
    uint32_t observed_udp_quality =
        udp_runtime_ != nullptr ? udp_runtime_->playout_quality_epoch() : 0;
    const auto fail_active_playback = [this]() {
        std::optional<PlaybackFact> fact;
        if (playback_state_.Fail(&fact) && fact.has_value()) {
            playback_enabled_ = false;
            PublishPlaybackFact(*fact);
        }
    };
    const auto observe_playout_quality = [this, &observed_wss_dropped,
                                          &observed_udp_quality]() {
        const uint32_t wss_dropped = wss_playback_queue_dropped_.load();
        const uint32_t udp_quality =
            udp_runtime_ != nullptr ? udp_runtime_->playout_quality_epoch() : 0;
        const bool degraded = wss_dropped != observed_wss_dropped ||
                              udp_quality != observed_udp_quality;
        observed_wss_dropped = wss_dropped;
        observed_udp_quality = udp_quality;
        if (degraded && playback_state_.active()) playback_state_.MarkDegraded();
    };
    if (playback_pcm_ == nullptr || playback_resampled_ == nullptr ||
        playback_resampled_capacity_ == 0) {
        events_.OnFailure("playback_buffer");
        running_ = false;
        MarkTaskStopped(kPlaybackStopped);
        vTaskDeleteWithCaps(nullptr);
        return;
    }
    while (running_) {
        observe_playout_quality();
        if (!ProcessPlaybackCommands()) {
            fail_active_playback();
            events_.OnFailure("playback_command");
            running_ = false;
            break;
        }
        udp::PlayoutFrame udp_frame;
        const bool using_udp = media_owner_ == voice::core::MediaOwner::kUdp;
        bool have_media = false;
        if (using_udp) {
            have_media = udp_runtime_ != nullptr && udp_runtime_->PollPlayout(&udp_frame);
        } else {
            have_media = xQueueReceive(playback_queue_, &packet, pdMS_TO_TICKS(10)) == pdTRUE;
        }

        uint32_t frame_generation = 0;
        uint32_t frame_sequence = 0;
        bool decode_plc = false;
        if (have_media) {
            frame_generation = using_udp ? udp_frame.generation : packet.generation;
            frame_sequence = using_udp ? udp_frame.sequence : packet.sequence;
            decode_plc = using_udp && udp_frame.kind == udp::PlayoutKind::kPlc;
        } else {
            std::optional<PlaybackFact> terminal_fact;
            const int64_t now_us = esp_timer_get_time();
            if (playback_state_.RequestFinalPlc(now_us, &frame_sequence)) {
                frame_generation = playback_generation_.load();
                decode_plc = true;
            } else if (playback_state_.ExpireDrain(now_us, &terminal_fact)) {
                playback_enabled_ = false;
                events_.OnConversationPhase(ConversationPhase::kListening);
                if (!terminal_fact.has_value() || !PublishPlaybackFact(*terminal_fact)) {
                    events_.OnFailure("playback_fact_queue");
                    running_ = false;
                    break;
                }
                continue;
            } else {
                // CONFIG_FREERTOS_HZ is 100; one tick avoids a priority-6 busy loop.
                if (using_udp) vTaskDelay(1);
                continue;
            }
        }
        if (!ProcessPlaybackCommands()) {
            fail_active_playback();
            events_.OnFailure("playback_command");
            running_ = false;
            break;
        }
        observe_playout_quality();
        if (!playback_state_.CanPlay(frame_generation)) continue;
        size_t samples = 0;
        const bool decoded = decode_plc
                                 ? codec_.DecodePlc60Ms(
                                       playback_pcm_.get(), kDecodedSamplesPerFrame, &samples)
                                 : codec_.Decode60Ms(
                                       using_udp ? udp_frame.payload.data() : packet.bytes.data(),
                                       using_udp ? udp_frame.payload_size : packet.size,
                                       playback_pcm_.get(), kDecodedSamplesPerFrame, &samples);
        if (!decoded || samples == 0) {
            fail_active_playback();
            events_.OnFailure("opus_decode");
            running_ = false;
            break;
        }
        if (samples != kDecodedSamplesPerFrame || playback_resampler_ == nullptr) {
            fail_active_playback();
            events_.OnFailure("playback_frame_size");
            running_ = false;
            break;
        }
        uint32_t resampled_samples = static_cast<uint32_t>(playback_resampled_capacity_);
        if (esp_ae_rate_cvt_process(
                playback_resampler_, playback_pcm_.get(), static_cast<uint32_t>(samples),
                playback_resampled_.get(), &resampled_samples) != ESP_AE_ERR_OK ||
            resampled_samples != kNominalResampledSamplesPerFrame) {
            fail_active_playback();
            events_.OnFailure("playback_resample");
            running_ = false;
            break;
        }
        constexpr size_t kInterruptibleSamples = 240;  // 10 ms at 24 kHz.
        const size_t total_resampled_samples = static_cast<size_t>(resampled_samples);
        size_t offset = 0;
        while (offset < total_resampled_samples && running_) {
            if (!ProcessPlaybackCommands()) {
                fail_active_playback();
                events_.OnFailure("playback_command");
                running_ = false;
                break;
            }
            // This is the final generation gate before the bounded I2S write.
            if (!playback_state_.CanPlay(frame_generation)) break;
            const size_t count =
                std::min(kInterruptibleSamples, total_resampled_samples - offset);
            if (pipeline_.WritePlayback(
                    {playback_resampled_.get() + offset, count, 24000, 1}, 20) !=
                audio::PortResult::kOk) {
                fail_active_playback();
                events_.OnFailure("playback_write");
                running_ = false;
                break;
            }
            offset += count;
            std::optional<PlaybackFact> fact;
            if (!playback_state_.RecordWritten(frame_sequence, count, &fact) ||
                (fact.has_value() && !PublishPlaybackFact(*fact))) {
                fail_active_playback();
                events_.OnFailure("playback_fact_queue");
                running_ = false;
                break;
            }
        }
        if (running_ && offset == total_resampled_samples &&
            playback_state_.CanPlay(frame_generation)) {
            std::optional<PlaybackFact> fact;
            if (!playback_state_.FinishFrame(frame_sequence, &fact) ||
                (fact.has_value() && !PublishPlaybackFact(*fact))) {
                fail_active_playback();
                events_.OnFailure("playback_completion");
                running_ = false;
            } else if (fact.has_value()) {
                playback_enabled_ = false;
                events_.OnConversationPhase(ConversationPhase::kListening);
            }
        }
        // WritePlayback is already paced by I2S. Sleeping for one 10 ms RTOS
        // tick here made each 60 ms packet take at least 70 ms to consume, so
        // a correctly paced downlink inevitably overflowed the WSS queues.
        // Yield to peers without changing the audio clock.
        taskYIELD();
    }
    ESP_LOGI(kLogTag, "playback task minimum free stack: %lu bytes",
             static_cast<unsigned long>(uxTaskGetStackHighWaterMark(nullptr)));
    MarkTaskStopped(kPlaybackStopped);
    vTaskDeleteWithCaps(nullptr);
}

bool VoiceRuntime::StartPlaybackResampler() {
    if (playback_resampler_ != nullptr) return false;
    esp_ae_rate_cvt_cfg_t config = {
        .src_rate = 16000,
        .dest_rate = 24000,
        .channel = 1,
        .bits_per_sample = 16,
        .complexity = 2,
        .perf_type = ESP_AE_RATE_CVT_PERF_TYPE_SPEED,
    };
    if (esp_ae_rate_cvt_open(&config, &playback_resampler_) != ESP_AE_ERR_OK ||
        playback_resampler_ == nullptr) {
        playback_resampler_ = nullptr;
        return false;
    }
    uint32_t required_samples = 0;
    if (esp_ae_rate_cvt_get_max_out_sample_num(
            playback_resampler_, kDecodedSamplesPerFrame, &required_samples) != ESP_AE_ERR_OK ||
        required_samples < kNominalResampledSamplesPerFrame ||
        required_samples > kMaximumResampledSamplesPerFrame) {
        ESP_LOGE(kLogTag, "invalid playback resampler capacity: %lu",
                 static_cast<unsigned long>(required_samples));
        StopPlaybackResampler();
        return false;
    }
    playback_pcm_.reset(new (std::nothrow) int16_t[kDecodedSamplesPerFrame]);
    playback_resampled_.reset(new (std::nothrow) int16_t[required_samples]);
    if (playback_pcm_ == nullptr || playback_resampled_ == nullptr) {
        ESP_LOGE(kLogTag, "playback buffer allocation failed");
        StopPlaybackResampler();
        return false;
    }
    playback_resampled_capacity_ = required_samples;
    ESP_LOGI(kLogTag, "playback resampler ready: 960 -> nominal 1440, capacity=%lu",
             static_cast<unsigned long>(required_samples));
    return true;
}

void VoiceRuntime::StopPlaybackResampler() {
    if (playback_resampler_ != nullptr) {
        esp_ae_rate_cvt_reset(playback_resampler_);
        esp_ae_rate_cvt_close(playback_resampler_);
        playback_resampler_ = nullptr;
    }
    playback_pcm_.reset();
    playback_resampled_.reset();
    playback_resampled_capacity_ = 0;
}

void VoiceRuntime::RecordStage(
    StageCounters* counters, uint32_t duration_us, uint32_t deadline_us) {
    if (counters == nullptr) return;
    counters->count.fetch_add(1, std::memory_order_relaxed);
    counters->total_us.fetch_add(duration_us, std::memory_order_relaxed);
    UpdateMaximum(&counters->max_us, duration_us);
    if (duration_us > deadline_us) {
        counters->deadline_misses.fetch_add(1, std::memory_order_relaxed);
    }
}

void VoiceRuntime::UpdateQueueHighWater(
    std::atomic<uint32_t>* high_water, QueueHandle_t queue) {
    if (high_water == nullptr || queue == nullptr) return;
    UpdateMaximum(high_water, static_cast<uint32_t>(uxQueueMessagesWaiting(queue)));
}

void VoiceRuntime::LogAndResetUplinkMetrics() {
    struct Snapshot final {
        uint32_t count;
        uint32_t average_us;
        uint32_t max_us;
        uint32_t deadline_misses;
    };
    const auto snapshot = [](StageCounters* counters) {
        const uint32_t count = counters->count.exchange(0, std::memory_order_relaxed);
        const uint32_t total_us = counters->total_us.exchange(0, std::memory_order_relaxed);
        return Snapshot{
            .count = count,
            .average_us = count == 0 ? 0 : total_us / count,
            .max_us = counters->max_us.exchange(0, std::memory_order_relaxed),
            .deadline_misses = counters->deadline_misses.exchange(0, std::memory_order_relaxed),
        };
    };
    const Snapshot capture = snapshot(&capture_stage_);
    const Snapshot framing = snapshot(&framing_stage_);
    const Snapshot encode = snapshot(&encode_stage_);
    const Snapshot send = snapshot(&send_stage_);
    ESP_LOGI(
        kLogTag,
        "uplink stages(count/avg_us/max_us/miss): capture=%lu/%lu/%lu/%lu frame=%lu/%lu/%lu/%lu "
        "encode=%lu/%lu/%lu/%lu send=%lu/%lu/%lu/%lu "
        "queues=%lu/%lu drops=%lu/%lu presend_stale=%lu wss_send_fail=%lu "
        "age_max_us=%lu/%lu local_send_completion_age_max_us=%lu",
        static_cast<unsigned long>(capture.count),
        static_cast<unsigned long>(capture.average_us),
        static_cast<unsigned long>(capture.max_us),
        static_cast<unsigned long>(capture.deadline_misses),
        static_cast<unsigned long>(framing.count),
        static_cast<unsigned long>(framing.average_us),
        static_cast<unsigned long>(framing.max_us),
        static_cast<unsigned long>(framing.deadline_misses),
        static_cast<unsigned long>(encode.count),
        static_cast<unsigned long>(encode.average_us),
        static_cast<unsigned long>(encode.max_us),
        static_cast<unsigned long>(encode.deadline_misses),
        static_cast<unsigned long>(send.count),
        static_cast<unsigned long>(send.average_us),
        static_cast<unsigned long>(send.max_us),
        static_cast<unsigned long>(send.deadline_misses),
        static_cast<unsigned long>(uplink_pcm_queue_high_water_.exchange(0)),
        static_cast<unsigned long>(uplink_encoded_queue_high_water_.exchange(0)),
        static_cast<unsigned long>(uplink_pcm_queue_dropped_.exchange(0)),
        static_cast<unsigned long>(uplink_encoded_queue_dropped_.exchange(0)),
        static_cast<unsigned long>(uplink_presend_stale_dropped_.exchange(0)),
        static_cast<unsigned long>(wss_uplink_send_failures_.exchange(0)),
        static_cast<unsigned long>(uplink_pcm_max_age_us_.exchange(0)),
        static_cast<unsigned long>(uplink_encoded_max_age_us_.exchange(0)),
        static_cast<unsigned long>(uplink_local_send_completion_max_age_us_.exchange(0)));
}

void VoiceRuntime::MarkTaskStopped(EventBits_t bit) {
    if (task_events_ != nullptr) xEventGroupSetBits(task_events_, bit);
}

}  // namespace rva::runtime
