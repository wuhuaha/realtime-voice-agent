#include "transport_wss/esp_websocket_client_port.h"

#include <algorithm>
#include <limits>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_websocket_client.h"
#include "freertos/FreeRTOS.h"

namespace rva::wss {
namespace {

constexpr char kLogTag[] = "rva-wss-port";
constexpr int kCloseOpcode = 0x08;
constexpr int kPingOpcode = 0x09;
constexpr int kPongOpcode = 0x0a;

esp_websocket_client_handle_t Native(void* handle) {
    return static_cast<esp_websocket_client_handle_t>(handle);
}

TickType_t TimeoutTicks(uint32_t timeout_ms) {
    return timeout_ms == 0 ? 0 : std::max<TickType_t>(1, pdMS_TO_TICKS(timeout_ms));
}

void EventHandler(void* argument, esp_event_base_t, int32_t event_id, void* event_data) {
    static_cast<EspIdfWebsocketClientPort*>(argument)->HandleNativeEvent(event_id, event_data);
}

}  // namespace

EspIdfWebsocketClientPort::~EspIdfWebsocketClientPort() {
    Destroy();
}

bool EspIdfWebsocketClientPort::Start() {
    if (native_handle_ == nullptr || sink_ == nullptr || destroyed_) return false;
    if (!registered_) {
        if (esp_websocket_register_events(Native(native_handle_), WEBSOCKET_EVENT_ANY, EventHandler, this) != ESP_OK) {
            return false;
        }
        registered_ = true;
    }
    return esp_websocket_client_start(Native(native_handle_)) == ESP_OK;
}

bool EspIdfWebsocketClientPort::SendText(const uint8_t* data, size_t size, uint32_t timeout_ms) {
    if (native_handle_ == nullptr || destroyed_ || size > static_cast<size_t>(std::numeric_limits<int>::max())) {
        return false;
    }
    return esp_websocket_client_send_text(
               Native(native_handle_), reinterpret_cast<const char*>(data), static_cast<int>(size),
               TimeoutTicks(timeout_ms)) == static_cast<int>(size);
}

bool EspIdfWebsocketClientPort::SendBinary(const uint8_t* data, size_t size, uint32_t timeout_ms) {
    if (native_handle_ == nullptr || destroyed_ || size > static_cast<size_t>(std::numeric_limits<int>::max())) {
        return false;
    }
    return esp_websocket_client_send_bin(
               Native(native_handle_), reinterpret_cast<const char*>(data), static_cast<int>(size),
               TimeoutTicks(timeout_ms)) == static_cast<int>(size);
}

bool EspIdfWebsocketClientPort::Close(uint32_t timeout_ms) {
    if (native_handle_ == nullptr || destroyed_) return true;
    if (!esp_websocket_client_is_connected(Native(native_handle_))) return true;
    return esp_websocket_client_close(Native(native_handle_), TimeoutTicks(timeout_ms)) == ESP_OK;
}

bool EspIdfWebsocketClientPort::Destroy() {
    if (native_handle_ == nullptr || destroyed_) return true;
    if (registered_) {
        if (esp_websocket_unregister_events(
                Native(native_handle_), WEBSOCKET_EVENT_ANY, EventHandler) != ESP_OK) {
            return false;
        }
        registered_ = false;
    }
    if (esp_websocket_client_destroy(Native(native_handle_)) != ESP_OK) return false;
    native_handle_ = nullptr;
    sink_ = nullptr;
    destroyed_ = true;
    return true;
}

void EspIdfWebsocketClientPort::HandleNativeEvent(int32_t event_id, void* event_data) noexcept {
    WssOwner* sink = sink_;
    if (sink == nullptr || destroyed_) return;
    if (event_id == WEBSOCKET_EVENT_CONNECTED) {
        ESP_LOGI(kLogTag, "websocket connected");
        sink->OnClientCallback({ClientEventType::kConnected});
        return;
    }
    if (event_id == WEBSOCKET_EVENT_DISCONNECTED || event_id == WEBSOCKET_EVENT_CLOSED) {
        ESP_LOGW(kLogTag, "websocket disconnected event_id=%ld", static_cast<long>(event_id));
        sink->OnClientCallback({ClientEventType::kDisconnected});
        return;
    }
    if (event_id == WEBSOCKET_EVENT_ERROR) {
        ESP_LOGW(kLogTag, "websocket error");
        sink->OnClientCallback({ClientEventType::kError});
        return;
    }
    if (event_id != WEBSOCKET_EVENT_DATA || event_data == nullptr) return;

    const auto* data = static_cast<const esp_websocket_event_data_t*>(event_data);
    // esp_websocket_client owns the WebSocket control-frame lifecycle, including
    // automatic PONG replies and separate close/disconnect events. Empty PING/PONG
    // frames are therefore transport activity, not malformed RVA application data.
    if (data->op_code == kCloseOpcode || data->op_code == kPingOpcode ||
        data->op_code == kPongOpcode) {
        return;
    }
    if (data->data_len <= 0 || data->payload_len <= 0 || data->payload_offset < 0) {
        sink->OnClientCallback({ClientEventType::kError});
        return;
    }
    ClientEventType type = ClientEventType::kError;
    if (data->op_code == 0x01) type = ClientEventType::kTextFragment;
    if (data->op_code == 0x02) type = ClientEventType::kBinaryFragment;
    if (data->op_code == 0x00 && data->payload_offset > 0) type = fragmented_type_;
    if (data->payload_offset == 0) fragmented_type_ = type;
    if (type == ClientEventType::kError) {
        sink->OnClientCallback({ClientEventType::kError});
        return;
    }
    sink->OnClientCallback({
        type,
        reinterpret_cast<const uint8_t*>(data->data_ptr),
        static_cast<size_t>(data->data_len),
        static_cast<size_t>(data->payload_offset),
        static_cast<size_t>(data->payload_len),
    });
}

}  // namespace rva::wss
