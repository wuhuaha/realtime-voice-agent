#pragma once

#include <cstddef>
#include <cstdint>

#include "transport_wss/wss_owner.h"

namespace rva::wss {

class EspIdfWebsocketClientPort final : public EspWebsocketClientPort {
public:
    explicit EspIdfWebsocketClientPort(void* native_handle) : native_handle_(native_handle) {}
    ~EspIdfWebsocketClientPort() override;

    void BindEventSink(WssOwner* sink) { sink_ = sink; }
    bool Start() override;
    bool SendText(const uint8_t* data, size_t size, uint32_t timeout_ms) override;
    bool SendBinary(const uint8_t* data, size_t size, uint32_t timeout_ms) override;
    bool Close(uint32_t timeout_ms) override;
    bool Destroy() override;

    void HandleNativeEvent(int32_t event_id, void* event_data) noexcept;

private:
    void* native_handle_ = nullptr;
    WssOwner* sink_ = nullptr;
    ClientEventType fragmented_type_ = ClientEventType::kError;
    bool registered_ = false;
    bool destroyed_ = false;
};

}  // namespace rva::wss
