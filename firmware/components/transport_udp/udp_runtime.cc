#include "transport_udp/udp_runtime.h"

#include <cstdlib>

#include <esp_timer.h>

namespace rva::udp {

UdpRuntime::UdpRuntime(UdpSession& session, DatagramIoPort& io)
    : session_(session), io_(io) {}

UdpRuntime::~UdpRuntime() {
    RequestStop();
    if (!JoinAndClose(1000)) std::abort();
}

bool UdpRuntime::Start() {
    if (closed_.load()) return false;
    if (task_.load() != nullptr) return true;
    events_ = xEventGroupCreate();
    if (events_ == nullptr) {
        JoinAndClose(0);
        return false;
    }
    stop_requested_.store(false);
    queue_dropped_.store(0);
    playout_queue_.Open(session_.generation());
    TaskHandle_t created = nullptr;
    if (xTaskCreate(TaskEntry, "rva_udp", 4096, this, 4, &created) != pdPASS) {
        JoinAndClose(0);
        return false;
    }
    task_.store(created);
    return true;
}

bool UdpRuntime::SendProbe() {
    std::lock_guard<std::mutex> lock(send_mutex_);
    std::array<uint8_t, wire::kMaxDatagramBytes> datagram{};
    size_t size = 0;
    if (stop_requested_.load()) return false;
    return session_.BuildProbe(datagram.data(), datagram.size(), &size) &&
           SendDatagram(datagram.data(), size);
}

bool UdpRuntime::SendKeepalive() {
    std::lock_guard<std::mutex> lock(send_mutex_);
    std::array<uint8_t, wire::kMaxDatagramBytes> datagram{};
    size_t size = 0;
    if (stop_requested_.load()) return false;
    return session_.BuildKeepalive(datagram.data(), datagram.size(), &size) &&
           SendDatagram(datagram.data(), size);
}

bool UdpRuntime::SendAudio(const uint8_t* opus, size_t opus_size,
                           uint32_t timestamp, uint32_t generation) {
    std::lock_guard<std::mutex> lock(send_mutex_);
    std::array<uint8_t, wire::kMaxDatagramBytes> datagram{};
    size_t size = 0;
    if (stop_requested_.load()) return false;
    return session_.BuildAudio(opus, opus_size, timestamp, generation,
                               datagram.data(), datagram.size(), &size) &&
           SendDatagram(datagram.data(), size);
}

bool UdpRuntime::FenceGeneration(uint32_t generation) {
    std::lock_guard<std::mutex> control_lock(control_mutex_);
    std::lock_guard<std::mutex> send_lock(send_mutex_);
    if (stop_requested_.load() || generation < session_.generation()) return false;
    playout_queue_.AdvanceGeneration(generation);
    return session_.FenceGeneration(generation);
}

bool UdpRuntime::AdvanceGeneration(uint32_t generation) {
    std::lock_guard<std::mutex> control_lock(control_mutex_);
    std::lock_guard<std::mutex> send_lock(send_mutex_);
    if (stop_requested_.load() || generation < session_.generation()) return false;
    playout_queue_.AdvanceGeneration(generation);
    return session_.AdvanceGeneration(generation);
}

bool UdpRuntime::PollPlayout(PlayoutFrame* frame) {
    return playout_queue_.Pop(frame);
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
        vTaskDelete(task);
        task_.store(nullptr);
    }
    if (!closed_.exchange(true)) io_.Close();
    playout_queue_.Close();
    if (events_ != nullptr) {
        vEventGroupDelete(events_);
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
    xEventGroupSetBits(events_, kExited);
}

bool UdpRuntime::SendDatagram(const uint8_t* data, size_t size) {
    const int sent = io_.Send(session_.server(), data, size);
    return sent == static_cast<int>(size);
}

}  // namespace rva::udp
