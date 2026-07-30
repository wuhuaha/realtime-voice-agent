#include "transport_udp/udp_runtime.h"

#include <cstdlib>
#include <new>

#include <esp_heap_caps.h>
#include <esp_log.h>
#include <esp_timer.h>
#include <freertos/idf_additions.h>

#include "transport_udp/endpoint_diagnostics.h"

namespace rva::udp {
namespace {

constexpr char kLogTag[] = "rva-udp-runtime";
// The UDP runtime task does more than socket receive: it authenticates packets
// with AES-GCM, updates replay/jitter state, and copies bounded Opus payloads
// into the playout queue. 4 KiB overflowed on ESP32-S3 during real UDP TTS
// downlink; keep this stack intentionally boring and measured rather than
// shaving bytes on the media control path.
constexpr uint32_t kRuntimeTaskStackBytes = 16 * 1024;

}  // namespace

void* UdpRuntime::operator new(size_t size) {
    void* pointer = heap_caps_malloc(size, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (pointer == nullptr) throw std::bad_alloc();
    return pointer;
}

void UdpRuntime::operator delete(void* pointer) noexcept {
    heap_caps_free(pointer);
}

UdpRuntime::UdpRuntime(UdpSession& session, DatagramIoPort& io)
    : session_(session), io_(io) {}

UdpRuntime::~UdpRuntime() {
    RequestStop();
    if (!JoinAndClose(1000)) {
        ESP_LOGE(kLogTag, "destructor close timed out; owner must fail closed");
    }
}

bool UdpRuntime::Start() {
    if (closed_.load()) return false;
    if (task_.load() != nullptr) return true;
    events_ = xEventGroupCreateWithCaps(MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (events_ == nullptr) {
        ESP_LOGW(kLogTag,
                 "start failed: event group allocation internal_free=%lu largest=%lu psram_free=%lu",
                 static_cast<unsigned long>(heap_caps_get_free_size(MALLOC_CAP_INTERNAL)),
                 static_cast<unsigned long>(heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL)),
                 static_cast<unsigned long>(heap_caps_get_free_size(MALLOC_CAP_SPIRAM)));
        JoinAndClose(0);
        return false;
    }
    stop_requested_.store(false);
    queue_dropped_.store(0);
    media_age_dropped_.store(0);
    playout_queue_.Open(session_.downlink_generation());
    TaskHandle_t created = nullptr;
    if (xTaskCreateWithCaps(TaskEntry, "rva_udp", kRuntimeTaskStackBytes, this, 4,
                            &created, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT) != pdPASS) {
        ESP_LOGW(kLogTag,
                 "start failed: task allocation internal_free=%lu largest=%lu psram_free=%lu",
                 static_cast<unsigned long>(heap_caps_get_free_size(MALLOC_CAP_INTERNAL)),
                 static_cast<unsigned long>(heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL)),
                 static_cast<unsigned long>(heap_caps_get_free_size(MALLOC_CAP_SPIRAM)));
        JoinAndClose(0);
        return false;
    }
    task_.store(created);
    return true;
}

bool UdpRuntime::SendProbe() {
    std::lock_guard<std::mutex> lock(send_mutex_);
    if (stop_requested_.load()) return false;
    std::array<uint8_t, wire::kMaxDatagramBytes> datagram{};
    size_t size = 0;
    return session_.BuildProbe(datagram.data(), datagram.size(), &size) &&
           SendDatagram(datagram.data(), size, true);
}

bool UdpRuntime::SendKeepalive() {
    std::lock_guard<std::mutex> lock(send_mutex_);
    std::array<uint8_t, wire::kMaxDatagramBytes> datagram{};
    size_t size = 0;
    if (stop_requested_.load()) return false;
    return session_.BuildKeepalive(datagram.data(), datagram.size(), &size) &&
           SendDatagram(datagram.data(), size, false);
}

bool UdpRuntime::SendAudio(
    const uint8_t* opus, size_t opus_size, uint32_t timestamp) {
    std::lock_guard<std::mutex> lock(send_mutex_);
    std::array<uint8_t, wire::kMaxDatagramBytes> datagram{};
    size_t size = 0;
    if (stop_requested_.load()) return false;
    return session_.BuildAudio(opus, opus_size, timestamp,
                               datagram.data(), datagram.size(), &size) &&
           SendDatagram(datagram.data(), size, false);
}

bool UdpRuntime::FenceGeneration(uint32_t generation) {
    std::lock_guard<std::mutex> control_lock(control_mutex_);
    std::lock_guard<std::mutex> send_lock(send_mutex_);
    if (stop_requested_.load() || generation < session_.downlink_generation()) return false;
    playout_queue_.AdvanceGeneration(generation);
    return session_.FenceGeneration(generation);
}

bool UdpRuntime::AdvanceGeneration(uint32_t generation) {
    std::lock_guard<std::mutex> control_lock(control_mutex_);
    std::lock_guard<std::mutex> send_lock(send_mutex_);
    if (stop_requested_.load() || generation < session_.downlink_generation()) return false;
    playout_queue_.AdvanceGeneration(generation);
    return session_.AdvanceGeneration(generation);
}

bool UdpRuntime::PollPlayout(PlayoutFrame* frame) {
    uint32_t expired = 0;
    const bool ready = playout_queue_.PopFresh(
        frame, esp_timer_get_time(), kMaximumMediaAgeUs, &expired);
    if (expired != 0) media_age_dropped_.fetch_add(expired);
    return ready;
}

void UdpRuntime::RequestStop() {
    std::lock_guard<std::mutex> control_lock(control_mutex_);
    std::lock_guard<std::mutex> lock(send_mutex_);
    stop_requested_.store(true);
    session_.Revoke();
    if (!closed_.load()) io_.Interrupt();
    playout_queue_.Close();
}

bool UdpRuntime::JoinAndClose(uint32_t timeout_ms) {
    const TaskHandle_t task = task_.load();
    if (task != nullptr) {
        if (xTaskGetCurrentTaskHandle() == task) return false;
        const EventBits_t bits = xEventGroupWaitBits(
            events_, kExited, pdFALSE, pdFALSE, pdMS_TO_TICKS(timeout_ms));
        if ((bits & kExited) == 0) return false;
        vTaskDeleteWithCaps(task);
        task_.store(nullptr);
    }
    if (!closed_.exchange(true)) io_.Close();
    playout_queue_.Close();
    if (events_ != nullptr) {
        vEventGroupDeleteWithCaps(events_);
        events_ = nullptr;
    }
    return true;
}

void UdpRuntime::TaskEntry(void* context) {
    static_cast<UdpRuntime*>(context)->Run();
    vTaskSuspend(nullptr);
}

void UdpRuntime::Run() {
    std::array<uint8_t, wire::kMaxDatagramBytes> datagram{};
    while (!stop_requested_.load()) {
        size_t size = 0;
        Endpoint source{};
        if (io_.Receive(datagram.data(), datagram.size(), &size, &source, 20)) {
            session_.Receive(source, datagram.data(), size, esp_timer_get_time());
        }
        for (int count = 0; count < 2; ++count) {
            PlayoutFrame frame = session_.PopPlayout(esp_timer_get_time());
            if (frame.kind == PlayoutKind::kNone) break;
            const auto pushed = playout_queue_.Push(frame);
            if (pushed == PlayoutPushResult::kFull) {
                queue_dropped_.fetch_add(1);
                break;
            }
        }
    }
    ESP_LOGI(kLogTag, "udp task minimum free stack: %lu bytes",
             static_cast<unsigned long>(uxTaskGetStackHighWaterMark(nullptr)));
    xEventGroupSetBits(events_, kExited);
}

bool UdpRuntime::SendDatagram(
    const uint8_t* data, size_t size, bool log_probe_outcome) {
    const DatagramSendOutcome outcome = io_.Send(session_.server(), data, size);
    const bool sent = outcome.sent_bytes == static_cast<int>(size);
    if (log_probe_outcome) {
        char peer_address[40]{};
        const bool peer_valid = FormatEndpointAddressForLog(
            session_.server(), peer_address, sizeof(peer_address));
        ESP_LOGI(kLogTag,
                 "udp_probe_send peer_address=%s peer_port=%u local_source_port=%u "
                 "socket_generation=%lu requested_bytes=%u sent_bytes=%d send_errno=%d outcome=%s",
                 peer_valid ? peer_address : "invalid",
                 static_cast<unsigned>(session_.server().port),
                 static_cast<unsigned>(outcome.local_source_port),
                 static_cast<unsigned long>(outcome.socket_generation),
                 static_cast<unsigned>(size), outcome.sent_bytes, outcome.error_code,
                 sent ? "sent" : "failed");
    }
    return sent;
}

}  // namespace rva::udp
