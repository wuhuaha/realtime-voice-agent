#pragma once

#include <cstddef>
#include <cstdint>
#include <memory>
#include <mutex>
#include <vector>

#include "voice_protocol/control.h"
#include "voice_protocol/media_header.h"

namespace rva::wss {

inline constexpr size_t kMaximumCallbackEvents = 8;
inline constexpr size_t kMaximumQueuedCallbackBytes = 65536;

enum class ClientEventType {
    kConnected,
    kDisconnected,
    kTextFragment,
    kBinaryFragment,
    kError,
};

struct ClientEventView final {
    ClientEventType type = ClientEventType::kError;
    const uint8_t* data = nullptr;
    size_t data_size = 0;
    size_t payload_offset = 0;
    size_t payload_size = 0;
};

struct OwnedClientEvent final {
    ClientEventType type = ClientEventType::kError;
    std::unique_ptr<uint8_t[]> data;
    size_t data_size = 0;
    size_t payload_offset = 0;
    size_t payload_size = 0;
};

class CallbackEventQueue final {
public:
    CallbackEventQueue(size_t capacity, size_t byte_capacity);

    bool TryPush(const ClientEventView& event) noexcept;
    bool TryPop(OwnedClientEvent* event);
    size_t size() const;
    size_t queued_bytes() const;
    uint32_t dropped_events() const;

private:
    const size_t capacity_;
    const size_t byte_capacity_;
    mutable std::mutex mutex_;
    std::vector<OwnedClientEvent> events_;
    size_t queued_bytes_ = 0;
    uint32_t dropped_events_ = 0;
};

enum class AssembleResult {
    kIncomplete,
    kComplete,
    kRejected,
};

class FrameAssembler final {
public:
    AssembleResult Consume(const OwnedClientEvent& event, std::vector<uint8_t>* frame);
    void Reset();

private:
    ClientEventType type_ = ClientEventType::kError;
    size_t expected_size_ = 0;
    std::vector<uint8_t> buffer_;
};

class EspWebsocketClientPort {
public:
    virtual ~EspWebsocketClientPort() = default;
    virtual bool Start() = 0;
    virtual bool SendText(const uint8_t* data, size_t size, uint32_t timeout_ms) = 0;
    virtual bool SendBinary(const uint8_t* data, size_t size, uint32_t timeout_ms) = 0;
    virtual bool Close(uint32_t timeout_ms) = 0;
    virtual bool Destroy() = 0;
};

class WssOwner final {
public:
    WssOwner(EspWebsocketClientPort& client, size_t event_capacity, size_t event_byte_capacity);
    ~WssOwner();

    bool Start();
    bool OnClientCallback(const ClientEventView& event) noexcept;
    bool Poll(OwnedClientEvent* event);
    bool SendText(const std::string& message, uint32_t timeout_ms);
    bool SendMedia(const uint8_t* frame, size_t size, uint32_t timeout_ms);
    void RequestClose() noexcept;
    bool SupervisorClose(uint32_t timeout_ms);

    uint32_t dropped_events() const { return events_.dropped_events(); }

private:
    EspWebsocketClientPort& client_;
    CallbackEventQueue events_;
    std::mutex state_mutex_;
    bool started_ = false;
    bool close_requested_ = false;
    bool destroyed_ = false;
};

}  // namespace rva::wss
