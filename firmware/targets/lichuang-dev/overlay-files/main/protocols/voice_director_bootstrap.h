#pragma once

#include "board.h"
#include "system_info.h"
#include "voice_agent_local_config.h"

#include <cJSON.h>
#include <esp_log.h>
#include <esp_timer.h>
#include <http_parser.h>
#include <http.h>

#include <algorithm>
#include <cmath>
#include <cctype>
#include <cstring>
#include <string>
#include <utility>

namespace voice_agent {

enum class DirectorBootstrapStatus {
    kDisabled,
    kSucceeded,
    kFailed,
};

struct DirectorBootstrapResult {
    DirectorBootstrapStatus status = DirectorBootstrapStatus::kDisabled;
    std::string worker_wss_url;
    std::string connect_grant;
    std::string worker_host;
};

namespace director_bootstrap_detail {

constexpr const char* kTag = "DirectorBootstrap";
constexpr int kTimeoutMs = 5000;
constexpr int kCleanupTimeoutMs = 250;
constexpr size_t kMaxResponseBytes = 8192;
constexpr size_t kMaxWorkerUrlBytes = 255;
constexpr size_t kMaxConnectGrantBytes = 4096;

inline bool IsIdentifier(const cJSON* value) {
    if (!cJSON_IsString(value) || value->valuestring == nullptr) {
        return false;
    }
    const size_t length = std::strlen(value->valuestring);
    if (length == 0 || length > 96 ||
        !std::isalnum(static_cast<unsigned char>(value->valuestring[0]))) {
        return false;
    }
    for (size_t index = 1; index < length; ++index) {
        const unsigned char character =
            static_cast<unsigned char>(value->valuestring[index]);
        if (!std::isalnum(character) && character != '_' && character != '.' &&
            character != ':' && character != '-') {
            return false;
        }
    }
    return true;
}

inline bool ParseUrl(const std::string& url, const char* first_scheme,
                     const char* second_scheme, const char* expected_path,
                     std::string& host) {
    if (url.empty() || url.size() > kMaxWorkerUrlBytes ||
        url.find('\0') != std::string::npos) {
        return false;
    }
    http_parser_url parsed;
    http_parser_url_init(&parsed);
    if (http_parser_parse_url(url.data(), url.size(), false, &parsed) != 0) {
        return false;
    }
    constexpr uint16_t kRequiredFields =
        (1U << UF_SCHEMA) | (1U << UF_HOST) | (1U << UF_PATH);
    if ((parsed.field_set & kRequiredFields) != kRequiredFields) {
        return false;
    }
    constexpr uint16_t kForbiddenFields =
        (1U << UF_USERINFO) | (1U << UF_QUERY) | (1U << UF_FRAGMENT);
    if ((parsed.field_set & kForbiddenFields) != 0) {
        return false;
    }
    const auto& scheme_field = parsed.field_data[UF_SCHEMA];
    const auto& host_field = parsed.field_data[UF_HOST];
    const auto& path_field = parsed.field_data[UF_PATH];
    const std::string scheme = url.substr(scheme_field.off, scheme_field.len);
    const std::string path = url.substr(path_field.off, path_field.len);
    if ((scheme != first_scheme && scheme != second_scheme) || host_field.len == 0) {
        return false;
    }
    if (path != expected_path) {
        return false;
    }
    host = url.substr(host_field.off, host_field.len);
    std::transform(host.begin(), host.end(), host.begin(),
                   [](unsigned char value) { return static_cast<char>(std::tolower(value)); });
    return true;
}

inline bool HasValidAllowedProfiles(const cJSON* profiles) {
    if (!cJSON_IsArray(profiles) || cJSON_GetArraySize(profiles) == 0) {
        return false;
    }
    bool saw_wss = false;
    bool saw_udp = false;
    const cJSON* profile = nullptr;
    cJSON_ArrayForEach(profile, profiles) {
        if (!cJSON_IsString(profile) || profile->valuestring == nullptr) {
            return false;
        }
        if (std::strcmp(profile->valuestring, "wss-opus-v1") == 0 && !saw_wss) {
            saw_wss = true;
        } else if (std::strcmp(profile->valuestring, "udp-opus-gcm-v1") == 0 && !saw_udp) {
            saw_udp = true;
        } else {
            return false;
        }
    }
    return saw_wss || saw_udp;
}

inline std::string BuildRequest(const std::string& device_id,
                                const std::string& transport_mode) {
    cJSON* root = cJSON_CreateObject();
    cJSON* profiles = cJSON_CreateArray();
    if (root == nullptr || profiles == nullptr ||
        !cJSON_AddStringToObject(root, "tenant_id", VOICE_AGENT_TENANT_ID) ||
        !cJSON_AddStringToObject(root, "device_id", device_id.c_str())) {
        cJSON_Delete(root);
        cJSON_Delete(profiles);
        return {};
    }
    if (transport_mode != "force_udp_for_test") {
        cJSON_AddItemToArray(profiles, cJSON_CreateString("wss-opus-v1"));
    }
    if (transport_mode != "force_wss") {
        cJSON_AddItemToArray(profiles, cJSON_CreateString("udp-opus-gcm-v1"));
    }
    if (!cJSON_AddItemToObject(root, "supported_profiles", profiles)) {
        cJSON_Delete(root);
        cJSON_Delete(profiles);
        return {};
    }
    char* serialized = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (serialized == nullptr) {
        return {};
    }
    std::string request(serialized);
    cJSON_free(serialized);
    return request;
}

inline bool ApplyRemainingTimeout(Http& http, int64_t deadline_us) {
    const int64_t remaining_us = deadline_us - esp_timer_get_time();
    if (remaining_us <= 0) {
        return false;
    }
    const int remaining_ms = static_cast<int>(
        std::min<int64_t>(kTimeoutMs, (remaining_us + 999) / 1000));
    http.SetTimeout(std::max(1, remaining_ms));
    return true;
}

inline void CloseWithinDeadline(Http& http, int64_t deadline_us) {
    const int64_t remaining_us = deadline_us - esp_timer_get_time();
    const int remaining_ms = remaining_us > 0
        ? static_cast<int>((remaining_us + 999) / 1000)
        : 1;
    http.SetTimeout(std::max(1, std::min(kCleanupTimeoutMs, remaining_ms)));
    http.Close();
}

inline bool ReadBounded(Http& http, std::string& body, int64_t deadline_us) {
    if (!ApplyRemainingTimeout(http, deadline_us)) {
        return false;
    }
    const size_t declared_length = http.GetBodyLength();
    if (declared_length > kMaxResponseBytes) {
        return false;
    }
    char buffer[512];
    while (true) {
        if (!ApplyRemainingTimeout(http, deadline_us)) {
            return false;
        }
        const int bytes_read = http.Read(buffer, sizeof(buffer));
        if (bytes_read < 0) {
            return false;
        }
        if (bytes_read == 0) {
            return true;
        }
        if (body.size() + static_cast<size_t>(bytes_read) > kMaxResponseBytes) {
            return false;
        }
        body.append(buffer, static_cast<size_t>(bytes_read));
    }
}

inline bool ParseResponse(const std::string& body, DirectorBootstrapResult& result) {
    const char* parse_end = nullptr;
    cJSON* root = cJSON_ParseWithLengthOpts(
        body.c_str(), body.size() + 1, &parse_end, true);
    if (!cJSON_IsObject(root)) {
        cJSON_Delete(root);
        return false;
    }
    const cJSON* worker_id = cJSON_GetObjectItemCaseSensitive(root, "worker_id");
    const cJSON* worker_url = cJSON_GetObjectItemCaseSensitive(root, "worker_wss_url");
    const cJSON* grant = cJSON_GetObjectItemCaseSensitive(root, "connect_grant");
    const cJSON* session_epoch = cJSON_GetObjectItemCaseSensitive(root, "session_epoch");
    const cJSON* fencing_token = cJSON_GetObjectItemCaseSensitive(root, "fencing_token");
    const cJSON* allowed_profiles = cJSON_GetObjectItemCaseSensitive(root, "allowed_profiles");
    const cJSON* expires_at = cJSON_GetObjectItemCaseSensitive(root, "expires_at");

    std::string worker_host;
    const bool valid_grant = cJSON_IsString(grant) && grant->valuestring != nullptr &&
        std::strlen(grant->valuestring) >= 32 &&
        std::strlen(grant->valuestring) <= kMaxConnectGrantBytes;
    const bool valid_fencing = cJSON_IsNumber(fencing_token) &&
        std::isfinite(fencing_token->valuedouble) && fencing_token->valuedouble >= 1 &&
        std::floor(fencing_token->valuedouble) == fencing_token->valuedouble;
    const bool valid_expiry = cJSON_IsNumber(expires_at) &&
        std::isfinite(expires_at->valuedouble) && expires_at->valuedouble > 0;
    const bool valid = IsIdentifier(worker_id) && IsIdentifier(session_epoch) &&
        cJSON_IsString(worker_url) && worker_url->valuestring != nullptr &&
        ParseUrl(worker_url->valuestring, "ws", "wss", "/v1/xiaozhi", worker_host) && valid_grant &&
        valid_fencing && HasValidAllowedProfiles(allowed_profiles) && valid_expiry;
    if (valid) {
        result.worker_wss_url = worker_url->valuestring;
        result.connect_grant = grant->valuestring;
        result.worker_host = std::move(worker_host);
    }
    cJSON_Delete(root);
    return valid;
}

}  // namespace director_bootstrap_detail

inline bool DevelopmentDirectFallbackEnabled() {
#if VOICE_AGENT_LOCAL_LAB
    return VOICE_AGENT_DEVELOPMENT_DIRECT_FALLBACK == 1;
#else
    return false;
#endif
}

inline DirectorBootstrapResult RequestDirectorBootstrap(
    const std::string& transport_mode) {
    DirectorBootstrapResult result;
    if (std::strcmp(VOICE_AGENT_BOOTSTRAP_MODE, "director") != 0) {
        return result;
    }
    result.status = DirectorBootstrapStatus::kFailed;

    std::string director_host;
    if (!director_bootstrap_detail::ParseUrl(
            VOICE_AGENT_DIRECTOR_URL, "http", "https",
            "/v1/session/bootstrap", director_host)) {
        ESP_LOGE(director_bootstrap_detail::kTag, "Director bootstrap configuration is invalid");
        return result;
    }

    const std::string device_id = SystemInfo::GetMacAddress();
    std::string request = director_bootstrap_detail::BuildRequest(device_id, transport_mode);
    if (request.empty()) {
        ESP_LOGE(director_bootstrap_detail::kTag, "Failed to allocate Director bootstrap request");
        return result;
    }

    auto http = Board::GetInstance().GetNetwork()->CreateHttp(2);
    if (http == nullptr) {
        ESP_LOGE(director_bootstrap_detail::kTag, "Failed to create Director HTTP client");
        return result;
    }
    const int64_t deadline_us = esp_timer_get_time() +
        static_cast<int64_t>(director_bootstrap_detail::kTimeoutMs) * 1000;
    http->SetResponseBodyLimit(director_bootstrap_detail::kMaxResponseBytes);
    http->SetTimeout(director_bootstrap_detail::kTimeoutMs);
    http->SetHeader("Authorization", std::string("Bearer ") + VOICE_AGENT_BOOTSTRAP_TOKEN);
    http->SetHeader("Content-Type", "application/json");
    http->SetHeader("Device-Id", device_id);
    http->SetHeader("Client-Id", Board::GetInstance().GetUuid());
    http->SetContent(std::move(request));

    ESP_LOGI(director_bootstrap_detail::kTag, "Requesting route from Director host=%s",
             director_host.c_str());
    if (!director_bootstrap_detail::ApplyRemainingTimeout(*http, deadline_us) ||
        !http->Open("POST", VOICE_AGENT_DIRECTOR_URL)) {
        ESP_LOGE(director_bootstrap_detail::kTag, "Director bootstrap connection failed");
        director_bootstrap_detail::CloseWithinDeadline(*http, deadline_us);
        return result;
    }
    if (!director_bootstrap_detail::ApplyRemainingTimeout(*http, deadline_us)) {
        ESP_LOGE(director_bootstrap_detail::kTag, "Director bootstrap deadline exceeded");
        director_bootstrap_detail::CloseWithinDeadline(*http, deadline_us);
        return result;
    }
    const int status_code = http->GetStatusCode();
    if (status_code != 200) {
        ESP_LOGE(director_bootstrap_detail::kTag,
                 "Director bootstrap rejected with status=%d", status_code);
        director_bootstrap_detail::CloseWithinDeadline(*http, deadline_us);
        return result;
    }
    std::string response;
    const bool read_ok = director_bootstrap_detail::ReadBounded(
        *http, response, deadline_us);
    director_bootstrap_detail::CloseWithinDeadline(*http, deadline_us);
    if (!read_ok || !director_bootstrap_detail::ParseResponse(response, result)) {
        ESP_LOGE(director_bootstrap_detail::kTag, "Director bootstrap response is invalid");
        return result;
    }
    result.status = DirectorBootstrapStatus::kSucceeded;
    ESP_LOGI(director_bootstrap_detail::kTag, "Director selected Worker host=%s",
             result.worker_host.c_str());
    return result;
}

}  // namespace voice_agent
