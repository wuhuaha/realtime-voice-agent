#include "native_runtime/config_adapters.h"

#include <algorithm>
#include <array>
#include <cstdio>
#include <cstring>
#include <ctime>

#include <esp_mac.h>
#include <esp_netif_sntp.h>
#include <esp_wifi.h>
#include <nvs_flash.h>

namespace rva::runtime {
namespace {

constexpr char kWifiNamespace[] = "rva_wifi";
constexpr char kWifiSsidKey[] = "ssid";
constexpr char kWifiPasswordKey[] = "password";
constexpr EventBits_t kConnectedBit = BIT0;
constexpr EventBits_t kFailedBit = BIT1;

config::ConfigResult FromEsp(esp_err_t error) {
    if (error == ESP_OK) return config::ConfigResult::kOk;
    if (error == ESP_ERR_NVS_NOT_FOUND) return config::ConfigResult::kNotFound;
    return config::ConfigResult::kStorageFailure;
}

config::ConfigResult ReadNvsString(const char* name_space, const char* key, std::string* value) {
    if (value == nullptr) return config::ConfigResult::kInvalidArgument;
    nvs_handle_t handle = 0;
    esp_err_t error = nvs_open(name_space, NVS_READONLY, &handle);
    if (error != ESP_OK) return FromEsp(error);
    size_t size = 0;
    error = nvs_get_str(handle, key, nullptr, &size);
    if (error == ESP_OK && size > 0 && size <= 4097) {
        std::vector<char> buffer(size);
        error = nvs_get_str(handle, key, buffer.data(), &size);
        if (error == ESP_OK) value->assign(buffer.data(), size - 1);
    }
    nvs_close(handle);
    return FromEsp(error);
}

}  // namespace

config::ConfigResult NvsConfigStore::ReadString(const char* key, std::string* value) {
    return ReadNvsString(config::kNvsNamespace, key, value);
}

config::ConfigResult NvsConfigStore::WriteString(const char* key, const std::string& value) {
    nvs_handle_t handle = 0;
    esp_err_t error = nvs_open(config::kNvsNamespace, NVS_READWRITE, &handle);
    if (error == ESP_OK) error = nvs_set_str(handle, key, value.c_str());
    if (error == ESP_OK) error = nvs_commit(handle);
    if (handle != 0) nvs_close(handle);
    return FromEsp(error);
}

config::ConfigResult NvsConfigStore::Erase(const char* key) {
    nvs_handle_t handle = 0;
    esp_err_t error = nvs_open(config::kNvsNamespace, NVS_READWRITE, &handle);
    if (error == ESP_OK) error = nvs_erase_key(handle, key);
    if (error == ESP_OK) error = nvs_commit(handle);
    if (handle != 0) nvs_close(handle);
    return FromEsp(error);
}

config::ConfigResult NvsSavedWifi::LoadSaved(std::vector<config::WifiCredential>* credentials) {
    if (credentials == nullptr) return config::ConfigResult::kInvalidArgument;
    std::string ssid;
    config::ConfigResult result = ReadNvsString(kWifiNamespace, kWifiSsidKey, &ssid);
    if (result != config::ConfigResult::kOk) return result;
    std::string password;
    result = ReadNvsString(kWifiNamespace, kWifiPasswordKey, &password);
    if (result == config::ConfigResult::kNotFound) result = config::ConfigResult::kOk;
    if (result != config::ConfigResult::kOk || ssid.empty() || ssid.size() > 32 || password.size() > 64) {
        return result == config::ConfigResult::kOk ? config::ConfigResult::kInvalidArgument : result;
    }
    credentials->push_back({std::move(ssid), std::move(password)});
    return config::ConfigResult::kOk;
}

config::ConfigResult NvsSavedWifi::Save(const config::WifiCredential& credential) {
    nvs_handle_t handle = 0;
    esp_err_t error = nvs_open(kWifiNamespace, NVS_READWRITE, &handle);
    // SSID 与密码必须在同一次 commit 中更新，避免掉电后拼出跨版本 credential。
    if (error == ESP_OK) error = nvs_set_str(handle, kWifiSsidKey, credential.ssid.c_str());
    if (error == ESP_OK) error = nvs_set_str(handle, kWifiPasswordKey, credential.password.c_str());
    if (error == ESP_OK) error = nvs_commit(handle);
    if (handle != 0) nvs_close(handle);
    return FromEsp(error);
}

WifiStation::~WifiStation() {
    Stop();
}

bool WifiStation::Start(config::WifiPlan plan) {
    return StartProvisioning() && Connect(std::move(plan));
}

bool WifiStation::Initialize() {
    if (started_) return true;
    events_ = xEventGroupCreate();
    if (events_ == nullptr) return false;
    const esp_err_t netif_result = esp_netif_init();
    const esp_err_t event_result = esp_event_loop_create_default();
    if ((netif_result != ESP_OK && netif_result != ESP_ERR_INVALID_STATE) ||
        (event_result != ESP_OK && event_result != ESP_ERR_INVALID_STATE)) {
        Stop();
        return false;
    }
    netif_ = esp_netif_create_default_wifi_sta();
    wifi_init_config_t initialization = WIFI_INIT_CONFIG_DEFAULT();
    if (netif_ == nullptr || esp_wifi_init(&initialization) != ESP_OK) {
        Stop();
        return false;
    }
    wifi_initialized_ = true;
    if (
        esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, EventHandler, this, &wifi_handler_) != ESP_OK ||
        esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, EventHandler, this, &ip_handler_) != ESP_OK ||
        esp_wifi_set_mode(WIFI_MODE_STA) != ESP_OK || esp_wifi_start() != ESP_OK) {
        Stop();
        return false;
    }
    started_ = true;
    return true;
}

bool WifiStation::StartProvisioning() {
    return Initialize();
}

bool WifiStation::Connect(config::WifiPlan plan) {
    if (!Initialize() || plan.credentials().empty()) return false;
    std::lock_guard<std::mutex> lock(connection_mutex_);
    plan_ = std::move(plan);
    credential_index_ = 0;
    attempts_ = 0;
    xEventGroupClearBits(events_, kConnectedBit | kFailedBit);
    wifi_ap_record_t current_ap{};
    if (esp_wifi_sta_get_ap_info(&current_ap) == ESP_OK) {
        reconnect_after_disconnect_ = true;
        if (esp_wifi_disconnect() == ESP_OK) return true;
        reconnect_after_disconnect_ = false;
    }
    return ConnectCurrent();
}

bool WifiStation::WaitConnected(uint32_t timeout_ms) {
    if (!started_ || events_ == nullptr) return false;
    const EventBits_t bits = xEventGroupWaitBits(
        events_, kConnectedBit | kFailedBit, pdFALSE, pdFALSE, pdMS_TO_TICKS(timeout_ms));
    return (bits & kConnectedBit) != 0;
}

void WifiStation::Stop() {
    if (started_) {
        esp_wifi_stop();
    }
    started_ = false;
    if (wifi_handler_ != nullptr) {
        esp_event_handler_instance_unregister(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_handler_);
        wifi_handler_ = nullptr;
    }
    if (ip_handler_ != nullptr) {
        esp_event_handler_instance_unregister(IP_EVENT, IP_EVENT_STA_GOT_IP, ip_handler_);
        ip_handler_ = nullptr;
    }
    if (wifi_initialized_) {
        esp_wifi_deinit();
        wifi_initialized_ = false;
    }
    if (netif_ != nullptr) {
        esp_netif_destroy_default_wifi(netif_);
        netif_ = nullptr;
    }
    if (events_ != nullptr) {
        vEventGroupDelete(events_);
        events_ = nullptr;
    }
}

bool WifiStation::Scan(std::vector<WifiScanRecord>* records) {
    if (records == nullptr || !Initialize()) return false;
    wifi_scan_config_t scan{};
    scan.show_hidden = false;
    if (esp_wifi_scan_start(&scan, true) != ESP_OK) return false;
    uint16_t count = 0;
    if (esp_wifi_scan_get_ap_num(&count) != ESP_OK) return false;
    count = std::min<uint16_t>(count, 20);
    std::vector<wifi_ap_record_t> raw(count);
    if (count > 0 && esp_wifi_scan_get_ap_records(&count, raw.data()) != ESP_OK) return false;
    records->clear();
    for (uint16_t index = 0; index < count; ++index) {
        const size_t length = strnlen(reinterpret_cast<const char*>(raw[index].ssid), sizeof(raw[index].ssid));
        if (length == 0 || length > 32) continue;
        std::string ssid(reinterpret_cast<const char*>(raw[index].ssid), length);
        const auto existing = std::find_if(records->begin(), records->end(), [&ssid](const WifiScanRecord& item) {
            return item.ssid == ssid;
        });
        const WifiScanRecord item{
            .ssid = std::move(ssid),
            .rssi = raw[index].rssi,
            .secured = raw[index].authmode != WIFI_AUTH_OPEN,
        };
        if (existing == records->end()) records->push_back(item);
        else if (item.rssi > existing->rssi) *existing = item;
    }
    std::sort(records->begin(), records->end(), [](const WifiScanRecord& left, const WifiScanRecord& right) {
        return left.rssi > right.rssi;
    });
    return true;
}

void WifiStation::EventHandler(void* context, esp_event_base_t base, int32_t id, void*) {
    static_cast<WifiStation*>(context)->HandleEvent(base, id);
}

void WifiStation::HandleEvent(esp_event_base_t base, int32_t id) {
    if (!started_) return;
    if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        xEventGroupSetBits(events_, kConnectedBit);
        return;
    }
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        std::lock_guard<std::mutex> lock(connection_mutex_);
        if (plan_.credentials().empty()) return;
        if (reconnect_after_disconnect_) {
            reconnect_after_disconnect_ = false;
            credential_index_ = 0;
            attempts_ = 0;
            if (!ConnectCurrent()) xEventGroupSetBits(events_, kFailedBit);
            return;
        }
        if (++attempts_ >= plan_.credentials().size() * 3) {
            xEventGroupSetBits(events_, kFailedBit);
            return;
        }
        credential_index_ = (credential_index_ + 1) % plan_.credentials().size();
        ConnectCurrent();
    }
}

bool WifiStation::ConnectCurrent() {
    const auto& credential = plan_.credentials()[credential_index_];
    wifi_config_t configuration{};
    if (credential.ssid.size() > sizeof(configuration.sta.ssid) ||
        credential.password.size() > sizeof(configuration.sta.password) - 1) {
        return false;
    }
    std::memcpy(configuration.sta.ssid, credential.ssid.data(), credential.ssid.size());
    std::memcpy(configuration.sta.password, credential.password.data(), credential.password.size());
    configuration.sta.threshold.authmode = credential.password.empty() ? WIFI_AUTH_OPEN : WIFI_AUTH_WPA2_PSK;
    return esp_wifi_set_config(WIFI_IF_STA, &configuration) == ESP_OK && esp_wifi_connect() == ESP_OK;
}

std::string DeviceIdFromStationMac() {
    std::array<uint8_t, 6> mac{};
    if (esp_read_mac(mac.data(), ESP_MAC_WIFI_STA) != ESP_OK) return {};
    std::array<char, 18> formatted{};
    std::snprintf(
        formatted.data(), formatted.size(), "%02x:%02x:%02x:%02x:%02x:%02x",
        mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    return formatted.data();
}

bool SynchronizeSystemClock(const char* server, uint32_t timeout_ms) {
    if (std::time(nullptr) >= 1577836800) return true;
    if (server == nullptr || server[0] == '\0' || timeout_ms == 0) return false;
    esp_sntp_config_t configuration = ESP_NETIF_SNTP_DEFAULT_CONFIG(server);
    if (esp_netif_sntp_init(&configuration) != ESP_OK) return false;
    const esp_err_t result = esp_netif_sntp_sync_wait(pdMS_TO_TICKS(timeout_ms));
    esp_netif_sntp_deinit();
    return result == ESP_OK && std::time(nullptr) >= 1577836800;
}

}  // namespace rva::runtime
