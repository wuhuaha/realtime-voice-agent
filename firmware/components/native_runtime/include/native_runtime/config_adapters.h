#pragma once

#include <atomic>
#include <mutex>
#include <string>
#include <vector>

#include <esp_event.h>
#include <esp_netif.h>
#include <freertos/FreeRTOS.h>
#include <freertos/event_groups.h>
#include <nvs.h>

#include "device_config/config_ports.h"
#include "device_config/device_config.h"

namespace rva::runtime {

class NvsConfigStore final : public config::ConfigStorePort {
public:
    config::ConfigResult ReadString(const char* key, std::string* value) override;
    config::ConfigResult WriteString(const char* key, const std::string& value) override;
    config::ConfigResult Erase(const char* key) override;
};

class NvsSavedWifi final : public config::SavedWifiPort {
public:
    config::ConfigResult LoadSaved(std::vector<config::WifiCredential>* credentials) override;
    config::ConfigResult Save(const config::WifiCredential& credential) override;
};

struct WifiScanRecord final {
    std::string ssid;
    int8_t rssi = -127;
    bool secured = true;
};

class WifiStation final {
public:
    WifiStation() = default;
    ~WifiStation();

    bool Start(config::WifiPlan plan);
    bool StartProvisioning();
    bool Connect(config::WifiPlan plan);
    bool WaitConnected(uint32_t timeout_ms);
    bool Scan(std::vector<WifiScanRecord>* records);
    void Stop();

private:
    static void EventHandler(void* context, esp_event_base_t base, int32_t id, void* data);
    void HandleEvent(esp_event_base_t base, int32_t id, void* data);
    bool ConnectCurrent();
    bool Initialize();

    config::WifiPlan plan_;
    esp_netif_t* netif_ = nullptr;
    esp_event_handler_instance_t wifi_handler_ = nullptr;
    esp_event_handler_instance_t ip_handler_ = nullptr;
    EventGroupHandle_t events_ = nullptr;
    size_t credential_index_ = 0;
    size_t attempts_ = 0;
    std::mutex connection_mutex_;
    std::atomic<bool> started_{false};
    bool reconnect_after_disconnect_ = false;
    bool wifi_initialized_ = false;
};

std::string DeviceIdFromStationMac();
bool SynchronizeSystemClock(const char* server, uint32_t timeout_ms);

}  // namespace rva::runtime
