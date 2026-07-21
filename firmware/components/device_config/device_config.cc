#include "device_config/device_config.h"

#include <algorithm>
#include <limits>
#include <string_view>
#include <utility>

namespace rva::config {
namespace {

bool IsAsciiControlOrSpace(unsigned char value) {
    return value <= 0x20 || value == 0x7f;
}

bool IsAsciiDigit(unsigned char value) {
    return value >= '0' && value <= '9';
}

bool IsAsciiAlpha(unsigned char value) {
    return (value >= 'a' && value <= 'z') || (value >= 'A' && value <= 'Z');
}

bool IsAsciiHexDigit(unsigned char value) {
    return IsAsciiDigit(value) || (value >= 'a' && value <= 'f') ||
           (value >= 'A' && value <= 'F');
}

bool ParsePort(std::string_view value, uint16_t* port) {
    if (value.empty()) {
        return false;
    }
    uint32_t parsed = 0;
    for (const unsigned char character : value) {
        if (!IsAsciiDigit(character)) {
            return false;
        }
        parsed = parsed * 10 + static_cast<uint32_t>(character - '0');
        if (parsed > std::numeric_limits<uint16_t>::max()) {
            return false;
        }
    }
    if (parsed == 0) {
        return false;
    }
    *port = static_cast<uint16_t>(parsed);
    return true;
}

std::string LowerAscii(std::string_view value) {
    std::string lowered(value);
    std::transform(lowered.begin(), lowered.end(), lowered.begin(), [](unsigned char character) {
        return character >= 'A' && character <= 'Z'
                   ? static_cast<char>(character + ('a' - 'A'))
                   : static_cast<char>(character);
    });
    return lowered;
}

bool IsValidHost(std::string_view host) {
    if (host.size() >= 2 && host.front() == '[' && host.back() == ']') {
        const std::string_view address = host.substr(1, host.size() - 2);
        return address.find(':') != std::string_view::npos &&
               std::all_of(address.begin(), address.end(), [](unsigned char character) {
                   return IsAsciiHexDigit(character) || character == ':' || character == '.';
               });
    }
    bool has_name_character = false;
    for (const unsigned char character : host) {
        if (IsAsciiAlpha(character) || IsAsciiDigit(character)) {
            has_name_character = true;
            continue;
        }
        if (character != '.' && character != '-' && character != '_') {
            return false;
        }
    }
    return has_name_character;
}

bool ReadOptional(ConfigStorePort& store, const char* key, std::string* value, ConfigResult* result) {
    *result = store.ReadString(key, value);
    if (*result == ConfigResult::kNotFound) {
        value->clear();
        return true;
    }
    return *result == ConfigResult::kOk;
}

bool ContainsSsid(const std::vector<WifiCredential>& credentials, const std::string& ssid) {
    return std::any_of(credentials.begin(), credentials.end(), [&ssid](const WifiCredential& candidate) {
        return candidate.ssid == ssid;
    });
}

}  // namespace

WifiPlan::WifiPlan(std::vector<WifiCredential> credentials) : credentials_(std::move(credentials)) {}

ConfigResult DeviceConfig::ParseEndpoint(const std::string& url, EndpointSnapshot* snapshot) {
    if (snapshot == nullptr || url.empty() || url.size() > kMaxServiceUrlBytes ||
        std::any_of(url.begin(), url.end(), [](unsigned char value) { return IsAsciiControlOrSpace(value); })) {
        return ConfigResult::kInvalidArgument;
    }

    const size_t scheme_end = url.find("://");
    if (scheme_end == std::string::npos) {
        return ConfigResult::kInvalidArgument;
    }
    const std::string scheme = LowerAscii(std::string_view(url).substr(0, scheme_end));
    if (scheme != "ws" && scheme != "wss" && scheme != "http" && scheme != "https") {
        return ConfigResult::kInvalidArgument;
    }
    if (url.find('#', scheme_end + 3) != std::string::npos) {
        return ConfigResult::kInvalidArgument;
    }

    const size_t authority_start = scheme_end + 3;
    const size_t authority_end = url.find_first_of("/?", authority_start);
    const std::string_view authority = std::string_view(url).substr(
        authority_start,
        authority_end == std::string::npos ? std::string::npos : authority_end - authority_start);
    if (authority.empty() || authority.find('@') != std::string_view::npos) {
        return ConfigResult::kInvalidArgument;
    }

    std::string_view host_view;
    std::string_view port_view;
    if (authority.front() == '[') {
        const size_t close = authority.find(']');
        if (close == std::string_view::npos || close == 1) {
            return ConfigResult::kInvalidArgument;
        }
        host_view = authority.substr(0, close + 1);
        if (close + 1 < authority.size()) {
            if (authority[close + 1] != ':') {
                return ConfigResult::kInvalidArgument;
            }
            port_view = authority.substr(close + 2);
        }
    } else {
        const size_t colon = authority.find(':');
        if (colon == std::string_view::npos) {
            host_view = authority;
        } else {
            if (authority.find(':', colon + 1) != std::string_view::npos) {
                return ConfigResult::kInvalidArgument;
            }
            host_view = authority.substr(0, colon);
            port_view = authority.substr(colon + 1);
        }
    }
    if (host_view.empty() || !IsValidHost(host_view)) {
        return ConfigResult::kInvalidArgument;
    }

    const bool secure = scheme == "wss" || scheme == "https";
    uint16_t port = secure ? 443 : 80;
    if (!port_view.empty() && !ParsePort(port_view, &port)) {
        return ConfigResult::kInvalidArgument;
    }
    if (authority.back() == ':') {
        return ConfigResult::kInvalidArgument;
    }

    EndpointSnapshot parsed;
    parsed.url = url;
    parsed.host = LowerAscii(host_view);
    parsed.port = port;
    parsed.secure = secure;
    parsed.origin = scheme + "://" + parsed.host + ":" + std::to_string(port);
    *snapshot = std::move(parsed);
    return ConfigResult::kOk;
}

ConfigResult DeviceConfig::ResolveCandidate(
    const EndpointCandidate& candidate,
    EndpointSource source,
    EndpointSnapshot* snapshot) {
    EndpointSnapshot parsed;
    const ConfigResult result = ParseEndpoint(candidate.url, &parsed);
    if (result != ConfigResult::kOk) {
        return result;
    }
    parsed.source = source;
    if (!candidate.token.empty() && candidate.token_origin == parsed.origin) {
        parsed.token = candidate.token;
    }
    *snapshot = std::move(parsed);
    return ConfigResult::kOk;
}

ConfigResult DeviceConfig::ResolveEndpoint(
    const std::vector<EndpointCandidate>& provisioned,
    EndpointSnapshot* snapshot) {
    if (snapshot == nullptr) {
        return ConfigResult::kInvalidArgument;
    }

    std::string saved_url;
    ConfigResult result = ConfigResult::kOk;
    if (!ReadOptional(endpoint_store_, kEndpointUrlKey, &saved_url, &result)) {
        return result;
    }
    if (!saved_url.empty()) {
        EndpointCandidate saved;
        saved.url = saved_url;
        if (!ReadOptional(endpoint_store_, kEndpointTokenKey, &saved.token, &result) ||
            !ReadOptional(endpoint_store_, kEndpointTokenOriginKey, &saved.token_origin, &result)) {
            return result;
        }
        if (ResolveCandidate(saved, EndpointSource::kSaved, snapshot) == ConfigResult::kOk) {
            // 本地只保存 endpoint；仅允许同 origin 的显式 provisioned token 补位，禁止跨环境转发 credential。
            if (snapshot->token.empty()) {
                for (const auto& candidate : provisioned) {
                    EndpointSnapshot fallback;
                    if (ResolveCandidate(candidate, EndpointSource::kProvisioned, &fallback) == ConfigResult::kOk &&
                        fallback.origin == snapshot->origin && !fallback.token.empty()) {
                        snapshot->token = fallback.token;
                        break;
                    }
                }
            }
            return ConfigResult::kOk;
        }
    }

    for (const auto& candidate : provisioned) {
        if (ResolveCandidate(candidate, EndpointSource::kProvisioned, snapshot) == ConfigResult::kOk) {
            return ConfigResult::kOk;
        }
    }
    return ConfigResult::kNotFound;
}

ConfigResult DeviceConfig::SaveEndpoint(const std::string& url) {
    EndpointSnapshot parsed;
    if (ParseEndpoint(url, &parsed) != ConfigResult::kOk) {
        return ConfigResult::kInvalidArgument;
    }

    std::string saved_url;
    std::string token_origin;
    ConfigResult result = ConfigResult::kOk;
    if (!ReadOptional(endpoint_store_, kEndpointUrlKey, &saved_url, &result) ||
        !ReadOptional(endpoint_store_, kEndpointTokenOriginKey, &token_origin, &result)) {
        return result;
    }
    EndpointSnapshot previous;
    const bool previous_valid = ParseEndpoint(saved_url, &previous) == ConfigResult::kOk;
    const bool origin_changed = previous_valid ? previous.origin != parsed.origin : token_origin != parsed.origin;
    if (origin_changed) {
        result = endpoint_store_.Erase(kEndpointTokenKey);
        if (result != ConfigResult::kOk && result != ConfigResult::kNotFound) {
            return result;
        }
        result = endpoint_store_.Erase(kEndpointTokenOriginKey);
        if (result != ConfigResult::kOk && result != ConfigResult::kNotFound) {
            return result;
        }
    }
    return endpoint_store_.WriteString(kEndpointUrlKey, url);
}

ConfigResult DeviceConfig::BindToken(const std::string& url, const std::string& token) {
    EndpointSnapshot parsed;
    if (token.empty() || ParseEndpoint(url, &parsed) != ConfigResult::kOk) {
        return ConfigResult::kInvalidArgument;
    }

    ConfigResult result = endpoint_store_.Erase(kEndpointTokenKey);
    if (result != ConfigResult::kOk && result != ConfigResult::kNotFound) {
        return result;
    }
    result = endpoint_store_.WriteString(kEndpointTokenOriginKey, parsed.origin);
    if (result != ConfigResult::kOk) {
        return result;
    }
    return endpoint_store_.WriteString(kEndpointTokenKey, token);
}

ConfigResult DeviceConfig::SaveWifi(const WifiCredential& credential) {
    if (credential.ssid.empty() || credential.ssid.size() > 32 ||
        credential.ssid.find('\0') != std::string::npos || credential.password.size() > 64 ||
        credential.password.find('\0') != std::string::npos ||
        (!credential.password.empty() && credential.password.size() < 8)) {
        return ConfigResult::kInvalidArgument;
    }
    return saved_wifi_.Save(credential);
}

ConfigResult DeviceConfig::LoadWifiPlan(
    const std::vector<WifiCredential>& provisioned,
    WifiPlan* plan) {
    if (plan == nullptr) {
        return ConfigResult::kInvalidArgument;
    }
    std::vector<WifiCredential> saved;
    const ConfigResult result = saved_wifi_.LoadSaved(&saved);
    if (result != ConfigResult::kOk && result != ConfigResult::kNotFound) {
        return result;
    }

    std::vector<WifiCredential> ordered;
    ordered.reserve(saved.size() + provisioned.size());
    for (const auto& credential : saved) {
        if (!credential.ssid.empty() && !ContainsSsid(ordered, credential.ssid)) {
            ordered.push_back(credential);
        }
    }
    for (const auto& credential : provisioned) {
        if (!credential.ssid.empty() && !ContainsSsid(ordered, credential.ssid)) {
            ordered.push_back(credential);
        }
    }
    if (ordered.empty()) {
        return ConfigResult::kNotFound;
    }
    *plan = WifiPlan(std::move(ordered));
    return ConfigResult::kOk;
}

}  // namespace rva::config
