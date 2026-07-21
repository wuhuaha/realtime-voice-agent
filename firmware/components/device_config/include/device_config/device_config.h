#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

#include "device_config/config_ports.h"

namespace rva::config {

inline constexpr size_t kMaxServiceUrlBytes = 255;
inline constexpr size_t kMaxWebsocketUrlBytes = kMaxServiceUrlBytes;
inline constexpr size_t kMaxNvsPhysicalKeyBytes = 15;
inline constexpr char kNvsNamespace[] = "voice_agent";
inline constexpr char kEndpointUrlKey[] = "ws_url";
inline constexpr char kEndpointTokenKey[] = "token";
inline constexpr char kEndpointTokenOriginKey[] = "token_origin";

static_assert(sizeof(kNvsNamespace) - 1 <= kMaxNvsPhysicalKeyBytes);
static_assert(sizeof(kEndpointUrlKey) - 1 <= kMaxNvsPhysicalKeyBytes);
static_assert(sizeof(kEndpointTokenKey) - 1 <= kMaxNvsPhysicalKeyBytes);
static_assert(sizeof(kEndpointTokenOriginKey) - 1 <= kMaxNvsPhysicalKeyBytes);

enum class EndpointSource {
    kSaved,
    kProvisioned,
};

struct EndpointCandidate final {
    std::string url;
    std::string token;
    std::string token_origin;
};

struct EndpointSnapshot final {
    std::string url;
    std::string host;
    std::string origin;
    std::string token;
    uint16_t port = 0;
    bool secure = false;
    EndpointSource source = EndpointSource::kProvisioned;
};

class WifiPlan final {
public:
    WifiPlan() = default;
    explicit WifiPlan(std::vector<WifiCredential> credentials);

    const std::vector<WifiCredential>& credentials() const { return credentials_; }

private:
    std::vector<WifiCredential> credentials_;
};

class DeviceConfig final {
public:
    DeviceConfig(ConfigStorePort& endpoint_store, SavedWifiPort& saved_wifi)
        : endpoint_store_(endpoint_store), saved_wifi_(saved_wifi) {}

    static ConfigResult ParseEndpoint(const std::string& url, EndpointSnapshot* snapshot);

    ConfigResult ResolveEndpoint(
        const std::vector<EndpointCandidate>& provisioned,
        EndpointSnapshot* snapshot);
    ConfigResult SaveEndpoint(const std::string& url);
    ConfigResult BindToken(const std::string& url, const std::string& token);
    ConfigResult SaveWifi(const WifiCredential& credential);
    ConfigResult LoadWifiPlan(
        const std::vector<WifiCredential>& provisioned,
        WifiPlan* plan);

private:
    ConfigResult ResolveCandidate(
        const EndpointCandidate& candidate,
        EndpointSource source,
        EndpointSnapshot* snapshot);

    ConfigStorePort& endpoint_store_;
    SavedWifiPort& saved_wifi_;
};

}  // namespace rva::config
