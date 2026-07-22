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

#include "voice_contracts/transport_profile.h"

namespace rva::runtime {
namespace {

constexpr EventBits_t kSupervisorStopped = BIT0;
constexpr EventBits_t kCaptureStopped = BIT1;
constexpr EventBits_t kUplinkStopped = BIT2;
constexpr EventBits_t kPlaybackStopped = BIT3;
constexpr uint32_t kAudioFailureLimit = 10;
// HIL measured about 26 KiB used through the first Opus encode. Keep roughly
// 10 KiB of headroom while returning scarce internal RAM to the WSS client.
constexpr uint32_t kUplinkTaskStackBytes = 36 * 1024;
// HIL measured roughly 20 KiB unused with the former 24 KiB stack.
constexpr uint32_t kCaptureTaskStackBytes = 8 * 1024;
// A 10 KiB stack overflowed on the first Opus decode/resample pass in HIL.
// Keep internal-RAM headroom now and tune down only from measured high-water data.
constexpr uint32_t kPlaybackTaskStackBytes = 20 * 1024;
constexpr uint32_t kWebsocketTeardownStackBytes = 6 * 1024;
constexpr uint32_t kWebsocketCloseTimeoutMs = 1000;
constexpr uint32_t kWebsocketTeardownTimeoutMs = 1500;
constexpr int64_t kResponseDrainGraceUs = 180000;
constexpr size_t kDecodedSamplesPerFrame = 960;
constexpr size_t kNominalResampledSamplesPerFrame = 1440;
constexpr size_t kMaximumResampledSamplesPerFrame = 4096;
constexpr char kLogTag[] = "rva-runtime";

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
    udp_expiry_deadline_us_ = 0;
    udp_heartbeat_interval_us_ = 0;
    udp_liveness_timeout_us_ = 0;
    udp_next_keepalive_us_ = 0;
    response_end_deadline_us_ = 0;
    fallback_to_wss_ = false;
    response_gate_.Reset();
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
    websocket.network_timeout_ms = 10000;
    websocket.ping_interval_sec = 15;
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
    owner_.reset(new (std::nothrow) wss::WssOwner(*client_port_, 8, 65536));
    if (owner_ == nullptr) {
        client_port_.reset();
        return false;
    }
    client_port_->BindEventSink(owner_.get());
    playback_queue_ = xQueueCreate(6, sizeof(MediaPacket));
    task_events_ = xEventGroupCreate();
    if (playback_queue_ == nullptr || task_events_ == nullptr || !codec_.Start() ||
        !pipeline_.Start().ok() || !StartPlaybackResampler()) {
        ESP_LOGE(kLogTag,
                 "start failed: resources queue=%d events=%d internal_free=%lu largest=%lu",
                 playback_queue_ != nullptr, task_events_ != nullptr,
                 static_cast<unsigned long>(heap_caps_get_free_size(MALLOC_CAP_INTERNAL)),
                 static_cast<unsigned long>(heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL)));
        StopPlaybackResampler();
        pipeline_.Stop();
        codec_.Stop();
        if (playback_queue_ != nullptr) vQueueDelete(playback_queue_);
        if (task_events_ != nullptr) vEventGroupDelete(task_events_);
        playback_queue_ = nullptr;
        task_events_ = nullptr;
        if (!CloseWebsocketBounded(kWebsocketTeardownTimeoutMs)) {
            FailClosedRestart("websocket_partial_start_teardown_failed");
        }
        owner_.reset();
        client_port_.reset();
        return false;
    }
    started_ = true;
    running_ = true;
    expected_task_bits_ = 0;
    // The supervisor performs bounded control-plane polling and does not own
    // DMA buffers. Its stack can live in PSRAM so audio and WSS tasks retain
    // deterministic internal-RAM stacks.
    bool tasks_started = xTaskCreateWithCaps(
                             SupervisorTask, "rva-supervisor", 8192, this, 6,
                             &supervisor_task_, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT) == pdPASS;
    if (!tasks_started) ESP_LOGE(kLogTag, "start failed: supervisor task");
    if (tasks_started) expected_task_bits_ |= kSupervisorStopped;
    if (tasks_started) {
        tasks_started = xTaskCreate(CaptureTask, "rva-capture", kCaptureTaskStackBytes, this, 7,
                                    &capture_task_) == pdPASS;
        if (!tasks_started) ESP_LOGE(kLogTag, "start failed: capture task");
        if (tasks_started) expected_task_bits_ |= kCaptureStopped;
    }
    if (tasks_started) {
        tasks_started = xTaskCreate(UplinkTask, "rva-uplink", kUplinkTaskStackBytes, this, 6,
                                    &uplink_task_) == pdPASS;
        if (!tasks_started) ESP_LOGE(kLogTag, "start failed: uplink task");
        if (tasks_started) expected_task_bits_ |= kUplinkStopped;
    }
    if (tasks_started) {
        tasks_started = xTaskCreate(PlaybackTask, "rva-playback", kPlaybackTaskStackBytes, this, 6,
                                    &playback_task_) == pdPASS;
        if (!tasks_started) ESP_LOGE(kLogTag, "start failed: playback task");
        if (tasks_started) expected_task_bits_ |= kPlaybackStopped;
    }
    ESP_LOGI(kLogTag, "runtime tasks ready: internal_free=%lu largest=%lu psram_free=%lu",
             static_cast<unsigned long>(heap_caps_get_free_size(MALLOC_CAP_INTERNAL)),
             static_cast<unsigned long>(heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL)),
             static_cast<unsigned long>(heap_caps_get_free_size(MALLOC_CAP_SPIRAM)));
    if (!tasks_started || !owner_->Start()) {
        if (tasks_started) ESP_LOGE(kLogTag, "start failed: websocket owner start");
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
    if (was_running && session_opened_ && owner_ != nullptr) {
        protocol::SessionOpened identity;
        {
            std::lock_guard<std::mutex> lock(identity_mutex_);
            identity = opened_;
        }
        protocol::SessionClose close{
            .session = identity.session,
            .reason = "normal",
            .initiated_by = "device",
            .detail = "",
        };
        std::string json;
        if (protocol::EncodeSessionClose(close, &json) == protocol::ControlError::kOk) {
            owner_->SendText(json, 250);
        }
    }
    if (owner_ != nullptr) owner_->RequestClose();
    if (udp_runtime_ != nullptr) udp_runtime_->RequestStop();
    if (task_events_ != nullptr && expected_task_bits_ != 0) {
        EventBits_t stopped = xEventGroupWaitBits(
            task_events_, expected_task_bits_, pdFALSE, pdTRUE,
            pdMS_TO_TICKS(5000));
        if ((stopped & expected_task_bits_) != expected_task_bits_) {
            // Tasks own codec/driver state while running, so forced deletion is unsafe.
            // A task that cannot leave its bounded loop also makes in-process recovery
            // unsafe; fail closed with a controlled restart instead of deadlocking the
            // application supervisor forever.
            FailClosedRestart("runtime_join_timeout");
        }
    }
    if (owner_ != nullptr && !CloseWebsocketBounded(kWebsocketTeardownTimeoutMs)) {
        FailClosedRestart("websocket_teardown_failed");
    }
    if (udp_runtime_ != nullptr && !udp_runtime_->JoinAndClose(1000)) {
        events_.OnFailure("udp_join_timeout");
    }
    StopPlaybackResampler();
    pipeline_.Stop();
    codec_.Stop();
    if (playback_queue_ != nullptr) {
        vQueueDelete(playback_queue_);
        playback_queue_ = nullptr;
    }
    if (task_events_ != nullptr) {
        vEventGroupDelete(task_events_);
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
    uplink_task_ = nullptr;
    playback_task_ = nullptr;
    expected_task_bits_ = 0;
    session_opened_ = false;
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
        runtime->HandleTaskAllocationFailure("capture", kCaptureStopped, false);
    } catch (...) {
        runtime->HandleTaskAllocationFailure("capture_exception", kCaptureStopped, false);
    }
}

void VoiceRuntime::UplinkTask(void* context) {
    auto* runtime = static_cast<VoiceRuntime*>(context);
    try {
        runtime->RunUplink();
    } catch (const std::bad_alloc&) {
        runtime->HandleTaskAllocationFailure("uplink", kUplinkStopped, false);
    } catch (...) {
        runtime->HandleTaskAllocationFailure("uplink_exception", kUplinkStopped, false);
    }
}

void VoiceRuntime::PlaybackTask(void* context) {
    auto* runtime = static_cast<VoiceRuntime*>(context);
    try {
        runtime->RunPlayback();
    } catch (const std::bad_alloc&) {
        runtime->HandleTaskAllocationFailure("playback", kPlaybackStopped, false);
    } catch (...) {
        runtime->HandleTaskAllocationFailure("playback_exception", kPlaybackStopped, false);
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
    uint32_t observed_udp_queue_dropped = 0;
    uint32_t observed_udp_media_age_dropped = 0;
    while (running_) {
        const uint32_t dropped_events = owner_->dropped_events();
        if (dropped_events != observed_dropped_events) {
            observed_dropped_events = dropped_events;
            events_.OnFailure("websocket_callback_overflow");
            running_ = false;
            continue;
        }
        const int64_t now_us = esp_timer_get_time();
        if (media_owner_ == voice::core::MediaOwner::kUdp && udp_runtime_ != nullptr) {
            const int64_t expiry_deadline_us = udp_expiry_deadline_us_.load();
            const int64_t liveness_timeout_us = udp_liveness_timeout_us_.load();
            const int64_t last_receive_us = udp_runtime_->last_authenticated_receive_us();
            const uint32_t queue_dropped =
                udp_runtime_->playout_queue_dropped() + udp_runtime_->stats().queue_dropped;
            const uint32_t media_age_dropped = udp_runtime_->playout_media_age_dropped();
            const char* failure = nullptr;
            if (expiry_deadline_us > 0 && now_us >= expiry_deadline_us) {
                failure = "udp_grant_expired";
            } else if (liveness_timeout_us > 0 && last_receive_us > 0 &&
                       now_us - last_receive_us >= liveness_timeout_us) {
                failure = "udp_media_inactive";
            } else if (queue_dropped != observed_udp_queue_dropped) {
                observed_udp_queue_dropped = queue_dropped;
                failure = "udp_playout_overflow";
            } else if (media_age_dropped != observed_udp_media_age_dropped) {
                observed_udp_media_age_dropped = media_age_dropped;
                failure = "udp_media_age";
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
        wss::OwnedClientEvent event;
        if (!owner_->Poll(&event)) {
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }
        if (event.type == wss::ClientEventType::kConnected) {
            events_.OnConnection(true);
            if (!SendSessionOpen()) {
                events_.OnFailure("session_open_send");
                running_ = false;
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
            opened->selected_media_profile == "udp-opus-gcm-v1"
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
        if (transcript->final && !response_gate_.active()) {
            events_.OnConversationPhase(ConversationPhase::kThinking);
        }
    } else if (const auto* response = std::get_if<protocol::ResponseEvent>(&message)) {
        if (response->type == protocol::ServerMessageType::kResponseBegin &&
            !core_gate_.AdvancePlaybackGeneration(response->generation)) {
            events_.OnFailure("playback_generation");
            running_ = false;
        } else if (response->type == protocol::ServerMessageType::kResponseBegin) {
            std::lock_guard<std::mutex> lock(playback_mutex_);
            if (udp_runtime_ != nullptr && !udp_runtime_->AdvanceGeneration(response->generation)) {
                events_.OnFailure("udp_generation");
                running_ = false;
            } else {
                playback_generation_ = response->generation;
            }
            if (running_) {
                response_end_deadline_us_ = 0;
                response_gate_.Begin(response->response_id, response->generation);
                playback_enabled_ = true;
                events_.OnConversationPhase(ConversationPhase::kSpeaking);
            }
        } else if (response->type == protocol::ServerMessageType::kResponseCancelled) {
            std::lock_guard<std::mutex> lock(playback_mutex_);
            xQueueReset(playback_queue_);
            if (response->generation == UINT32_MAX ||
                (udp_runtime_ != nullptr && !udp_runtime_->FenceGeneration(response->generation + 1))) {
                events_.OnFailure("cancel_generation");
                running_ = false;
            } else {
                playback_generation_ = response->generation + 1;
            }
            playback_enabled_ = false;
            response_end_deadline_us_ = 0;
            response_gate_.End();
        }
        if (response->type == protocol::ServerMessageType::kResponseEnd) {
            std::lock_guard<std::mutex> lock(playback_mutex_);
            response_gate_.End();
            response_end_deadline_us_ = esp_timer_get_time() + kResponseDrainGraceUs;
        } else if (response->type == protocol::ServerMessageType::kResponseCancelled) {
            events_.OnConversationPhase(ConversationPhase::kListening);
        }
        if (response->type == protocol::ServerMessageType::kResponseText) {
            events_.OnResponseText(response->text.c_str());
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
        events_.OnFailure("wss_media_admission");
        running_ = false;
        return;
    }
    MediaPacket packet;
    packet.size = static_cast<uint16_t>(header.payload_length);
    packet.generation = header.generation;
    std::memcpy(packet.bytes.data(), frame.data() + protocol::kMediaHeaderBytes, packet.size);
    if (xQueueSend(playback_queue_, &packet, 0) != pdTRUE) {
        events_.OnFailure("playback_queue_full");
        running_ = false;
    }
}

bool VoiceRuntime::SendSessionOpen() {
    protocol::SessionOpen open;
    open.request_id = open_request_id_;
    open.device_id = device_id_;
    open.supported_media_profiles = {"wss-opus-v2", "udp-opus-gcm-v1"};
    open.preferred_media_profile =
        preferred_media_ == MediaPreference::kUdp ? "udp-opus-gcm-v1" : "wss-opus-v2";
    open.capabilities = {
        config_.aec,
        config_.vad,
        false,
        config_.display,
        config_.touch,
    };
    std::string json;
    return protocol::EncodeSessionOpen(open, &json) == protocol::ControlError::kOk && owner_->SendText(json, 1000);
}

bool VoiceRuntime::ConfigureUdp(const protocol::SessionOpened& opened) {
    if (!opened.udp_grant.has_value()) return false;
    const protocol::UdpGrant& control = *opened.udp_grant;
    const auto now = std::chrono::system_clock::now();
    const auto now_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                            now.time_since_epoch())
                            .count();
    if (now_ms < 1577836800000LL || static_cast<uint64_t>(now_ms) >= control.expires_at_ms) {
        return false;
    }
    const uint64_t remaining_ms = control.expires_at_ms - static_cast<uint64_t>(now_ms);

    auto uplink = std::make_unique<udp::MbedTlsGcm>();
    auto downlink = std::make_unique<udp::MbedTlsGcm>();
    auto session = std::make_unique<udp::UdpSession>(*uplink, *downlink);
    auto io = std::make_unique<UdpSocketPort>();
    udp::SessionGrant grant;
    if (!io->Open(control.host, control.port, &grant.server)) return false;
    grant.media_id = opened.media_id;
    grant.media_epoch = opened.media_epoch;
    grant.initial_generation = playback_generation_;
    grant.uplink_key = control.uplink_key;
    grant.downlink_key = control.downlink_key;
    grant.uplink_salt = control.uplink_salt;
    grant.downlink_salt = control.downlink_salt;
    if (!session->Configure(grant)) return false;
    auto runtime = std::make_unique<udp::UdpRuntime>(*session, *io);
    if (!runtime->Start() || !runtime->SendProbe()) return false;
    const int64_t deadline_us = esp_timer_get_time() +
                                static_cast<int64_t>(control.probe_timeout_ms) * 1000;
    while (running_ && !session->ready() && esp_timer_get_time() < deadline_us) {
        vTaskDelay(pdMS_TO_TICKS(10));
    }
    if (!session->ready()) {
        runtime->RequestStop();
        runtime->JoinAndClose(1000);
        return false;
    }
    udp_uplink_crypto_ = std::move(uplink);
    udp_downlink_crypto_ = std::move(downlink);
    udp_session_ = std::move(session);
    udp_io_ = std::move(io);
    udp_runtime_ = std::move(runtime);
    const int64_t now_us = esp_timer_get_time();
    const uint64_t maximum_remaining_ms =
        static_cast<uint64_t>((std::numeric_limits<int64_t>::max() - now_us) / 1000);
    udp_expiry_deadline_us_ = remaining_ms > maximum_remaining_ms
                                  ? std::numeric_limits<int64_t>::max()
                                  : now_us + static_cast<int64_t>(remaining_ms) * 1000;
    return true;
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
            const audio::PortResult fed = pipeline_.FeedFrontend(
                {capture.samples, capture.sample_count, capture.sample_rate_hz, capture.channel_count});
            consecutive_failures = fed == audio::PortResult::kOk ? 0 : consecutive_failures + 1;
        } else {
            consecutive_failures++;
        }
        if (consecutive_failures >= kAudioFailureLimit) {
            events_.OnFailure("capture_pipeline_failure");
            running_ = false;
        }
    }
    MarkTaskStopped(kCaptureStopped);
    vTaskDelete(nullptr);
}

void VoiceRuntime::RunUplink() {
    std::vector<int16_t> fetched(frontend_.fetch_samples_per_channel());
    std::array<int16_t, 960> accumulated{};
    size_t accumulated_samples = 0;
    std::array<uint8_t, protocol::kWssMaxPayloadBytes> opus{};
    uint32_t consecutive_failures = 0;
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
        if (frontend_.ConsumeSpeechStarted() && !CancelActiveResponseOnSpeech()) {
            events_.OnFailure("barge_in_cancel");
            running_ = false;
            continue;
        }
        size_t offset = 0;
        while (offset < output.sample_count && running_) {
            if (accumulated_samples == accumulated.size() && !session_opened_) {
                // Keep capture bounded while admission is pending. Retaining a full
                // frame here would make copied == 0 and spin this high-priority task.
                accumulated_samples = 0;
            }
            const size_t copied = std::min(accumulated.size() - accumulated_samples, output.sample_count - offset);
            std::copy_n(output.samples + offset, copied, accumulated.begin() + accumulated_samples);
            accumulated_samples += copied;
            offset += copied;
            if (accumulated_samples != accumulated.size() || !session_opened_) continue;
            size_t opus_size = 0;
            if (!codec_.Encode60Ms(accumulated.data(), accumulated.size(), opus.data(), opus.size(), &opus_size)) {
                events_.OnFailure("opus_encode");
                accumulated_samples = 0;
                continue;
            }
            protocol::SessionOpened identity;
            {
                std::lock_guard<std::mutex> lock(identity_mutex_);
                identity = opened_;
            }
            const uint32_t timestamp = uplink_timestamp_;
            uplink_timestamp_ += 960;
            if (opus_size == 0) {
                accumulated_samples = 0;
                continue;
            }
            if (media_owner_ == voice::core::MediaOwner::kUdp) {
                if (udp_runtime_ == nullptr ||
                    !udp_runtime_->SendAudio(
                        opus.data(), opus_size, timestamp, playback_generation_)) {
                    events_.OnFailure("udp_uplink_send");
                    fallback_to_wss_ = true;
                    running_ = false;
                }
            } else if (media_owner_ == voice::core::MediaOwner::kWss) {
                protocol::MediaHeader header;
                header.flags = 1;
                header.media_id = identity.media_id;
                header.media_epoch = identity.media_epoch;
                header.sequence = uplink_sequence_++;
                header.timestamp = timestamp;
                header.generation = 0;
                header.payload_length = static_cast<uint32_t>(opus_size);
                std::array<uint8_t, protocol::kWssMaxFrameBytes> frame{};
                if (protocol::SerializeMediaHeader(header, protocol::MediaDirection::kUplink, frame.data()) !=
                        protocol::MediaError::kOk) {
                    events_.OnFailure("uplink_header");
                    running_ = false;
                } else {
                    std::memcpy(frame.data() + protocol::kMediaHeaderBytes, opus.data(), opus_size);
                    if (!owner_->SendMedia(frame.data(), protocol::kMediaHeaderBytes + opus_size, 1000)) {
                        events_.OnFailure("uplink_send");
                        running_ = false;
                    }
                }
            } else {
                if (session_opened_) {
                    events_.OnFailure("uplink_send");
                    running_ = false;
                }
            }
            accumulated_samples = 0;
        }
    }
    ESP_LOGI(kLogTag, "uplink task minimum free stack: %lu bytes",
             static_cast<unsigned long>(uxTaskGetStackHighWaterMark(nullptr)));
    MarkTaskStopped(kUplinkStopped);
    vTaskDelete(nullptr);
}

bool VoiceRuntime::CancelActiveResponseOnSpeech() {
    protocol::SessionIdentity session_identity;
    protocol::CancelTarget target;
    // 先标记再发送；发送失败会重连，避免不确定重试导致同一代重复取消。
    {
        std::lock_guard<std::mutex> lock(playback_mutex_);
        if (response_gate_.PrepareCancel(&target)) {
            const uint32_t generation = playback_generation_.load();
            xQueueReset(playback_queue_);
            if (udp_runtime_ != nullptr && !udp_runtime_->FenceGeneration(generation)) return false;
            playback_enabled_ = false;
            // The remote cancel is sent after releasing the playback lock.
        } else if (response_end_deadline_us_.load() > 0) {
            const uint32_t generation = playback_generation_.load();
            xQueueReset(playback_queue_);
            if (udp_runtime_ != nullptr && !udp_runtime_->FenceGeneration(generation)) {
                return false;
            }
            playback_enabled_ = false;
            response_end_deadline_us_ = 0;
            events_.OnConversationPhase(ConversationPhase::kListening);
            return true;
        } else {
            return true;
        }
    }
    {
        std::lock_guard<std::mutex> lock(identity_mutex_);
        session_identity = opened_.session;
    }
    std::string json;
    return protocol::EncodeResponseCancel(session_identity, target, "barge_in", &json) ==
               protocol::ControlError::kOk &&
           owner_ != nullptr && owner_->SendText(json, 250);
}

bool VoiceRuntime::CompleteResponseDrainIfDue(int64_t now_us) {
    std::lock_guard<std::mutex> lock(playback_mutex_);
    const int64_t deadline_us = response_end_deadline_us_.load();
    if (deadline_us <= 0 || now_us < deadline_us) return true;

    const uint32_t generation = playback_generation_.load();
    xQueueReset(playback_queue_);
    if (udp_runtime_ != nullptr && !udp_runtime_->FenceGeneration(generation)) return false;
    playback_enabled_ = false;
    response_end_deadline_us_ = 0;
    response_gate_.End();
    events_.OnConversationPhase(ConversationPhase::kListening);
    return true;
}

void VoiceRuntime::RunPlayback() {
    MediaPacket packet;
    if (playback_pcm_ == nullptr || playback_resampled_ == nullptr ||
        playback_resampled_capacity_ == 0) {
        events_.OnFailure("playback_buffer");
        running_ = false;
        MarkTaskStopped(kPlaybackStopped);
        vTaskDelete(nullptr);
        return;
    }
    while (running_) {
        if (!CompleteResponseDrainIfDue(esp_timer_get_time())) {
            events_.OnFailure("drain_generation");
            running_ = false;
            break;
        }
        udp::PlayoutFrame udp_frame;
        const bool using_udp = media_owner_ == voice::core::MediaOwner::kUdp;
        if (using_udp) {
            if (udp_runtime_ == nullptr || !udp_runtime_->PollPlayout(&udp_frame)) {
                vTaskDelay(pdMS_TO_TICKS(5));
                continue;
            }
        } else if (xQueueReceive(playback_queue_, &packet, pdMS_TO_TICKS(100)) != pdTRUE) {
            continue;
        }
        size_t samples = 0;
        const bool decoded = using_udp && udp_frame.kind == udp::PlayoutKind::kPlc
                                 ? codec_.DecodePlc60Ms(
                                       playback_pcm_.get(), kDecodedSamplesPerFrame, &samples)
                                 : codec_.Decode60Ms(
                                       using_udp ? udp_frame.payload.data() : packet.bytes.data(),
                                       using_udp ? udp_frame.payload_size : packet.size,
                                       playback_pcm_.get(), kDecodedSamplesPerFrame, &samples);
        if (!decoded || samples == 0) {
            events_.OnFailure("opus_decode");
            continue;
        }
        if (samples != kDecodedSamplesPerFrame || playback_resampler_ == nullptr) {
            events_.OnFailure("playback_frame_size");
            running_ = false;
            break;
        }
        uint32_t resampled_samples = static_cast<uint32_t>(playback_resampled_capacity_);
        if (esp_ae_rate_cvt_process(
                playback_resampler_, playback_pcm_.get(), static_cast<uint32_t>(samples),
                playback_resampled_.get(), &resampled_samples) != ESP_AE_ERR_OK ||
            resampled_samples != kNominalResampledSamplesPerFrame) {
            events_.OnFailure("playback_resample");
            running_ = false;
            break;
        }
        const uint32_t frame_generation = using_udp ? udp_frame.generation : packet.generation;
        constexpr size_t kInterruptibleSamples = 240;  // 10 ms at 24 kHz.
        const size_t total_resampled_samples = static_cast<size_t>(resampled_samples);
        size_t offset = 0;
        while (offset < total_resampled_samples && running_) {
            if (!playback_enabled_ || frame_generation != playback_generation_) break;
            const size_t count =
                std::min(kInterruptibleSamples, total_resampled_samples - offset);
            if (pipeline_.WritePlayback(
                    {playback_resampled_.get() + offset, count, 24000, 1}, 20) !=
                audio::PortResult::kOk) {
                events_.OnFailure("playback_write");
                running_ = false;
                break;
            }
            offset += count;
        }
    }
    ESP_LOGI(kLogTag, "playback task minimum free stack: %lu bytes",
             static_cast<unsigned long>(uxTaskGetStackHighWaterMark(nullptr)));
    MarkTaskStopped(kPlaybackStopped);
    vTaskDelete(nullptr);
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

void VoiceRuntime::MarkTaskStopped(EventBits_t bit) {
    if (task_events_ != nullptr) xEventGroupSetBits(task_events_, bit);
}

}  // namespace rva::runtime
