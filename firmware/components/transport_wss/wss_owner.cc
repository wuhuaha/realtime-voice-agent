#include "transport_wss/wss_owner.h"

#include <algorithm>
#include <cstring>
#include <new>
#include <utility>

namespace rva::wss {
namespace {

size_t MaximumPayload(ClientEventType type) {
    if (type == ClientEventType::kTextFragment) return protocol::kMaxControlBytes;
    if (type == ClientEventType::kBinaryFragment) return protocol::kWssMaxFrameBytes;
    return 0;
}

}  // namespace

CallbackEventQueue::CallbackEventQueue(size_t capacity, size_t byte_capacity)
    : capacity_(std::min(capacity, kMaximumCallbackEvents)),
      byte_capacity_(std::min(byte_capacity, kMaximumQueuedCallbackBytes)) {
    events_.reserve(capacity_);
}

bool CallbackEventQueue::TryPush(const ClientEventView& event) noexcept {
    const size_t maximum = MaximumPayload(event.type);
    const bool carries_data = event.type == ClientEventType::kTextFragment ||
                              event.type == ClientEventType::kBinaryFragment;
    if ((carries_data &&
         (event.data == nullptr || event.data_size == 0 || event.payload_size == 0 ||
          event.payload_size > maximum || event.payload_offset > event.payload_size ||
          event.data_size > event.payload_size - event.payload_offset)) ||
        (!carries_data && (event.data_size != 0 || event.payload_offset != 0 || event.payload_size != 0))) {
        std::lock_guard<std::mutex> lock(mutex_);
        ++dropped_events_;
        return false;
    }
    OwnedClientEvent owned;
    owned.type = event.type;
    owned.payload_offset = event.payload_offset;
    owned.payload_size = event.payload_size;
    owned.data_size = event.data_size;
    if (carries_data) {
        owned.data.reset(new (std::nothrow) uint8_t[event.data_size]);
        if (owned.data == nullptr) {
            std::lock_guard<std::mutex> lock(mutex_);
            ++dropped_events_;
            return false;
        }
        std::memcpy(owned.data.get(), event.data, event.data_size);
    }

    std::lock_guard<std::mutex> lock(mutex_);
    if (capacity_ == 0 || events_.size() >= capacity_ ||
        owned.data_size > byte_capacity_ - std::min(byte_capacity_, queued_bytes_)) {
        ++dropped_events_;
        return false;
    }
    queued_bytes_ += owned.data_size;
    events_.push_back(std::move(owned));
    return true;
}

bool CallbackEventQueue::TryPop(OwnedClientEvent* event) {
    if (event == nullptr) return false;
    std::lock_guard<std::mutex> lock(mutex_);
    if (events_.empty()) return false;
    queued_bytes_ -= events_.front().data_size;
    *event = std::move(events_.front());
    events_.erase(events_.begin());
    return true;
}

size_t CallbackEventQueue::size() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return events_.size();
}

size_t CallbackEventQueue::queued_bytes() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return queued_bytes_;
}

uint32_t CallbackEventQueue::dropped_events() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return dropped_events_;
}

AssembleResult FrameAssembler::Consume(const OwnedClientEvent& event, std::vector<uint8_t>* frame) {
    if (frame == nullptr || MaximumPayload(event.type) == 0 || event.payload_size > MaximumPayload(event.type) ||
        event.data == nullptr || event.data_size == 0 ||
        event.payload_offset + event.data_size > event.payload_size) {
        Reset();
        return AssembleResult::kRejected;
    }
    if (event.payload_offset == 0) {
        Reset();
        type_ = event.type;
        expected_size_ = event.payload_size;
        buffer_.reserve(expected_size_);
    }
    if (type_ != event.type || expected_size_ != event.payload_size || event.payload_offset != buffer_.size()) {
        Reset();
        return AssembleResult::kRejected;
    }
    buffer_.insert(buffer_.end(), event.data.get(), event.data.get() + event.data_size);
    if (buffer_.size() != expected_size_) return AssembleResult::kIncomplete;
    *frame = std::move(buffer_);
    Reset();
    return AssembleResult::kComplete;
}

void FrameAssembler::Reset() {
    type_ = ClientEventType::kError;
    expected_size_ = 0;
    buffer_.clear();
}

WssOwner::WssOwner(EspWebsocketClientPort& client, size_t event_capacity, size_t event_byte_capacity)
    : client_(client), events_(event_capacity, event_byte_capacity) {}

WssOwner::~WssOwner() {
    if (!destroyed_) SupervisorClose(1000);
}

bool WssOwner::Start() {
    std::lock_guard<std::mutex> lock(state_mutex_);
    if (started_ || close_requested_ || destroyed_) return false;
    started_ = client_.Start();
    return started_;
}

bool WssOwner::OnClientCallback(const ClientEventView& event) noexcept {
    return events_.TryPush(event);
}

bool WssOwner::Poll(OwnedClientEvent* event) {
    return events_.TryPop(event);
}

bool WssOwner::SendText(const std::string& message, uint32_t timeout_ms) {
    std::lock_guard<std::mutex> lock(state_mutex_);
    return started_ && !close_requested_ && message.size() <= protocol::kMaxControlBytes &&
           client_.SendText(reinterpret_cast<const uint8_t*>(message.data()), message.size(), timeout_ms);
}

bool WssOwner::SendMedia(const uint8_t* frame, size_t size, uint32_t timeout_ms) {
    std::lock_guard<std::mutex> lock(state_mutex_);
    return started_ && !close_requested_ && frame != nullptr && size <= protocol::kWssMaxFrameBytes &&
           client_.SendBinary(frame, size, timeout_ms);
}

void WssOwner::RequestClose() noexcept {
    std::lock_guard<std::mutex> lock(state_mutex_);
    close_requested_ = true;
}

bool WssOwner::SupervisorClose(uint32_t timeout_ms) {
    std::lock_guard<std::mutex> lock(state_mutex_);
    if (destroyed_) return true;
    close_requested_ = true;
    const bool closed = !started_ || client_.Close(timeout_ms);
    const bool destroyed = client_.Destroy();
    if (destroyed) {
        destroyed_ = true;
        started_ = false;
    }
    return closed && destroyed;
}

}  // namespace rva::wss
