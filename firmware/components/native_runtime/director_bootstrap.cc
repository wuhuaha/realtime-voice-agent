#include "native_runtime/director_bootstrap.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <memory>
#include <new>
#include <string_view>
#include <vector>

#include <esp_crt_bundle.h>
#include <esp_http_client.h>
#include <esp_log.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

#include "cJSON.h"
#include "device_config/device_config.h"

namespace rva::runtime {
namespace {

constexpr size_t kMaximumResponseBytes = 8192;
constexpr uint64_t kMaximumJsonInteger = 9007199254740991ULL;
constexpr uint32_t kReleaseAttempts = 2;
constexpr int kReleaseTimeoutMs = 800;
constexpr uint32_t kReleaseRetryDelayMs = 75;
constexpr char kLogTag[] = "rva-bootstrap";

struct JsonDeleter {
    void operator()(cJSON* value) const { cJSON_Delete(value); }
};
using Json = std::unique_ptr<cJSON, JsonDeleter>;

struct ResponseBuffer {
    std::vector<uint8_t> bytes;
    bool rejected = false;
};

esp_err_t HttpEvent(esp_http_client_event_t* event) noexcept {
    auto* response = static_cast<ResponseBuffer*>(event->user_data);
    if (event->event_id != HTTP_EVENT_ON_DATA || event->data_len <= 0 || response == nullptr) return ESP_OK;
    const size_t length = static_cast<size_t>(event->data_len);
    if (response->rejected || length > kMaximumResponseBytes - response->bytes.size()) {
        response->rejected = true;
        return ESP_FAIL;
    }
    const auto* data = static_cast<const uint8_t*>(event->data);
    try {
        response->bytes.insert(response->bytes.end(), data, data + length);
        return ESP_OK;
    } catch (...) {
        response->rejected = true;
        return ESP_ERR_NO_MEM;
    }
}

bool GetString(cJSON* root, const char* field, size_t maximum, std::string* output) {
    const cJSON* value = cJSON_GetObjectItemCaseSensitive(root, field);
    if (!cJSON_IsString(value) || value->valuestring == nullptr) return false;
    const size_t size = std::strlen(value->valuestring);
    if (size == 0 || size > maximum) return false;
    output->assign(value->valuestring, size);
    return true;
}

bool GetPositiveInteger(cJSON* root, const char* field, uint64_t* output) {
    const cJSON* value = cJSON_GetObjectItemCaseSensitive(root, field);
    if (!cJSON_IsNumber(value) || !std::isfinite(value->valuedouble) || value->valuedouble < 1.0 ||
        value->valuedouble > static_cast<double>(kMaximumJsonInteger) ||
        std::floor(value->valuedouble) != value->valuedouble) {
        return false;
    }
    *output = static_cast<uint64_t>(value->valuedouble);
    return true;
}

bool IsIdentifier(std::string_view value) {
    return !value.empty() && value.size() <= 96 &&
           std::all_of(value.begin(), value.end(), [](unsigned char character) {
               return (character >= 'a' && character <= 'z') ||
                      (character >= 'A' && character <= 'Z') ||
                      (character >= '0' && character <= '9') || character == '.' ||
                      character == '_' || character == ':' || character == '-';
           });
}

bool StartsWith(std::string_view value, std::string_view prefix) {
    return value.size() >= prefix.size() && value.substr(0, prefix.size()) == prefix;
}

bool EndsWith(std::string_view value, std::string_view suffix) {
    return value.size() >= suffix.size() && value.substr(value.size() - suffix.size()) == suffix;
}

void ResetGrant(BootstrapGrant* grant) noexcept {
    if (grant == nullptr) return;
    grant->worker_id.clear();
    grant->worker_wss_url.clear();
    grant->connect_grant.clear();
    grant->session_epoch.clear();
    grant->fencing_token = 0;
}

void CommitReleaseIdentity(BootstrapGrant* grant, BootstrapGrant* parsed) noexcept {
    grant->worker_id.swap(parsed->worker_id);
    grant->session_epoch.swap(parsed->session_epoch);
    grant->fencing_token = parsed->fencing_token;
    parsed->fencing_token = 0;
}

}  // namespace

bool BootstrapGrant::HasReleaseIdentity() const noexcept {
    return IsIdentifier(worker_id) && IsIdentifier(session_epoch) && fencing_token > 0 &&
           fencing_token <= kMaximumJsonInteger;
}

bool DirectorBootstrap::Request(
    const std::string& url,
    const std::string& authorization_token,
    const std::string& tenant_id,
    const std::string& device_id,
    BootstrapGrant* grant) noexcept {
    ResetGrant(grant);
    try {
        if (grant == nullptr || authorization_token.empty() || !IsIdentifier(tenant_id) ||
            !IsIdentifier(device_id) || url.size() < 8 || url.size() > 512 ||
            !EndsWith(url, "/v1/session/bootstrap")) {
            return false;
        }
    Json request(cJSON_CreateObject());
    if (request == nullptr || cJSON_AddStringToObject(request.get(), "tenant_id", tenant_id.c_str()) == nullptr ||
        cJSON_AddStringToObject(request.get(), "device_id", device_id.c_str()) == nullptr ||
        cJSON_AddStringToObject(request.get(), "control_protocol", "rva-control-v2") == nullptr) {
        return false;
    }
    cJSON* profiles = cJSON_AddArrayToObject(request.get(), "supported_profiles");
    if (profiles == nullptr || cJSON_AddItemToArray(profiles, cJSON_CreateString("wss-opus-v3")) == 0 ||
        cJSON_AddItemToArray(profiles, cJSON_CreateString("udp-opus-gcm-v2")) == 0) return false;
    std::unique_ptr<char, decltype(&cJSON_free)> body(cJSON_PrintUnformatted(request.get()), cJSON_free);
    if (body == nullptr) return false;

    ResponseBuffer response;
    response.bytes.reserve(kMaximumResponseBytes);
    esp_http_client_config_t configuration{};
    configuration.url = url.c_str();
    configuration.method = HTTP_METHOD_POST;
    configuration.timeout_ms = 10000;
    configuration.event_handler = HttpEvent;
    configuration.user_data = &response;
    if (StartsWith(url, "https://")) configuration.crt_bundle_attach = esp_crt_bundle_attach;
    esp_http_client_handle_t client = esp_http_client_init(&configuration);
    if (client == nullptr) {
        ESP_LOGE(kLogTag, "request failed: client_init");
        return false;
    }
    const std::string authorization = "Bearer " + authorization_token;
    const bool configured =
        esp_http_client_set_header(client, "Content-Type", "application/json") == ESP_OK &&
        esp_http_client_set_header(client, "Authorization", authorization.c_str()) == ESP_OK &&
        esp_http_client_set_post_field(
            client, body.get(), static_cast<int>(std::strlen(body.get()))) == ESP_OK;
    const esp_err_t perform_result = configured ? esp_http_client_perform(client) : ESP_FAIL;
    const int status_code = esp_http_client_get_status_code(client);
    esp_http_client_cleanup(client);
    if (!configured) {
        ESP_LOGE(kLogTag, "request failed: client_config");
        return false;
    }
    if (perform_result != ESP_OK) {
        ESP_LOGE(kLogTag, "request failed: transport=%s status=%d bytes=%u",
                 esp_err_to_name(perform_result), status_code,
                 static_cast<unsigned>(response.bytes.size()));
        return false;
    }
    if (status_code != 200 || response.rejected || response.bytes.empty()) {
        ESP_LOGE(kLogTag, "request rejected: status=%d bytes=%u overflow=%d", status_code,
                 static_cast<unsigned>(response.bytes.size()), response.rejected ? 1 : 0);
        return false;
    }

    Json root(cJSON_ParseWithLength(
        reinterpret_cast<const char*>(response.bytes.data()), response.bytes.size()));
    if (root == nullptr) {
        ESP_LOGE(kLogTag, "response invalid: json bytes=%u",
                 static_cast<unsigned>(response.bytes.size()));
        return false;
    }
    BootstrapGrant parsed;
    std::string control_protocol;
    if (!GetString(root.get(), "worker_id", 96, &parsed.worker_id) || !IsIdentifier(parsed.worker_id) ||
        !GetString(root.get(), "session_epoch", 96, &parsed.session_epoch) || !IsIdentifier(parsed.session_epoch) ||
        !GetPositiveInteger(root.get(), "fencing_token", &parsed.fencing_token)) {
        ESP_LOGE(kLogTag, "response invalid: lease_identity");
        return false;
    }

    // From this point onward the caller can release the exact Director lease,
    // even when the remaining media fields are incompatible or malformed.
    CommitReleaseIdentity(grant, &parsed);
    if (!GetString(root.get(), "worker_wss_url", 255, &grant->worker_wss_url) ||
        !GetString(root.get(), "connect_grant", 4096, &grant->connect_grant) || grant->connect_grant.size() < 32 ||
        !GetString(root.get(), "control_protocol", 32, &control_protocol) || control_protocol != "rva-control-v2") {
        ESP_LOGE(kLogTag, "response invalid: media_fields");
        return false;
    }
    config::EndpointSnapshot endpoint;
    if (config::DeviceConfig::ParseEndpoint(grant->worker_wss_url, &endpoint) != config::ConfigResult::kOk ||
        !EndsWith(grant->worker_wss_url, "/v2/voice")) {
        ESP_LOGE(kLogTag, "response invalid: worker_endpoint");
        return false;
    }
    const cJSON* allowed = cJSON_GetObjectItemCaseSensitive(root.get(), "allowed_profiles");
    bool supports_wss = false;
    if (cJSON_IsArray(allowed)) {
        const cJSON* item = nullptr;
        cJSON_ArrayForEach(item, allowed) {
            supports_wss |= cJSON_IsString(item) && std::strcmp(item->valuestring, "wss-opus-v3") == 0;
        }
    }
    if (!supports_wss) {
        ESP_LOGE(kLogTag, "response invalid: allowed_profiles");
        return false;
    }
    ESP_LOGI(kLogTag, "bootstrap accepted: status=200 bytes=%u",
             static_cast<unsigned>(response.bytes.size()));
    return true;
    } catch (const std::bad_alloc&) {
        ESP_LOGE(kLogTag, "request failed: allocation");
        return false;
    } catch (...) {
        ESP_LOGE(kLogTag, "request failed: unexpected_exception");
        return false;
    }
}

bool DirectorBootstrap::Release(
    const std::string& bootstrap_url,
    const std::string& authorization_token,
    const std::string& tenant_id,
    const std::string& device_id,
    const BootstrapGrant& grant) noexcept {
    try {
        if (authorization_token.empty() || !IsIdentifier(tenant_id) || !IsIdentifier(device_id) ||
            !grant.HasReleaseIdentity() || bootstrap_url.size() < 8 || bootstrap_url.size() > 512 ||
            !EndsWith(bootstrap_url, "/v1/session/bootstrap")) {
            return false;
        }
    std::string release_url = bootstrap_url;
    release_url.replace(
        release_url.size() - std::strlen("/v1/session/bootstrap"),
        std::strlen("/v1/session/bootstrap"),
        "/v1/session/release");

    Json request(cJSON_CreateObject());
    if (request == nullptr || cJSON_AddStringToObject(request.get(), "tenant_id", tenant_id.c_str()) == nullptr ||
        cJSON_AddStringToObject(request.get(), "device_id", device_id.c_str()) == nullptr ||
        cJSON_AddStringToObject(request.get(), "worker_id", grant.worker_id.c_str()) == nullptr ||
        cJSON_AddStringToObject(request.get(), "session_epoch", grant.session_epoch.c_str()) == nullptr ||
        cJSON_AddNumberToObject(
            request.get(), "fencing_token", static_cast<double>(grant.fencing_token)) == nullptr) {
        return false;
    }
    std::unique_ptr<char, decltype(&cJSON_free)> body(cJSON_PrintUnformatted(request.get()), cJSON_free);
    if (body == nullptr) return false;

    const std::string authorization = "Bearer " + authorization_token;
    for (uint32_t attempt = 1; attempt <= kReleaseAttempts; ++attempt) {
        ResponseBuffer response;
        response.bytes.reserve(128);
        esp_http_client_config_t configuration{};
        configuration.url = release_url.c_str();
        configuration.method = HTTP_METHOD_POST;
        configuration.timeout_ms = kReleaseTimeoutMs;
        configuration.event_handler = HttpEvent;
        configuration.user_data = &response;
        if (StartsWith(release_url, "https://")) configuration.crt_bundle_attach = esp_crt_bundle_attach;
        esp_http_client_handle_t client = esp_http_client_init(&configuration);
        if (client == nullptr) {
            ESP_LOGW(kLogTag, "route release unavailable: client_init attempt=%lu",
                     static_cast<unsigned long>(attempt));
        } else {
            const bool configured =
                esp_http_client_set_header(client, "Content-Type", "application/json") == ESP_OK &&
                esp_http_client_set_header(client, "Authorization", authorization.c_str()) == ESP_OK &&
                esp_http_client_set_post_field(
                    client, body.get(), static_cast<int>(std::strlen(body.get()))) == ESP_OK;
            const esp_err_t perform_result = configured ? esp_http_client_perform(client) : ESP_FAIL;
            const int status_code = esp_http_client_get_status_code(client);
            esp_http_client_cleanup(client);
            if (configured && perform_result == ESP_OK && status_code == 200 && !response.rejected) {
                ESP_LOGI(kLogTag, "route release acknowledged attempt=%lu",
                         static_cast<unsigned long>(attempt));
                return true;
            }
            ESP_LOGW(kLogTag, "route release unavailable: transport=%s status=%d attempt=%lu",
                     esp_err_to_name(perform_result), status_code,
                     static_cast<unsigned long>(attempt));
        }
        if (attempt < kReleaseAttempts) {
            vTaskDelay(pdMS_TO_TICKS(kReleaseRetryDelayMs));
        }
    }
    return false;
    } catch (const std::bad_alloc&) {
        ESP_LOGW(kLogTag, "route release unavailable: allocation");
        return false;
    } catch (...) {
        ESP_LOGW(kLogTag, "route release unavailable: unexpected_exception");
        return false;
    }
}

}  // namespace rva::runtime
