#include "device_config/device_config.h"

#include <cassert>
#include <map>
#include <string>
#include <utility>
#include <vector>

namespace {

using rva::config::ConfigResult;
using rva::config::ConfigStorePort;
using rva::config::DeviceConfig;
using rva::config::EndpointCandidate;
using rva::config::EndpointSnapshot;
using rva::config::EndpointSource;
using rva::config::SavedWifiPort;
using rva::config::WifiCredential;
using rva::config::WifiPlan;

class MemoryStore final : public ConfigStorePort {
public:
    ConfigResult ReadString(const char* key, std::string* value) override {
        if (read_result != ConfigResult::kOk) {
            return read_result;
        }
        const auto found = values.find(key);
        if (found == values.end()) {
            return ConfigResult::kNotFound;
        }
        *value = found->second;
        return ConfigResult::kOk;
    }

    ConfigResult WriteString(const char* key, const std::string& value) override {
        events.push_back(std::string("write:") + key);
        if (write_result != ConfigResult::kOk) {
            return write_result;
        }
        values[key] = value;
        return ConfigResult::kOk;
    }

    ConfigResult Erase(const char* key) override {
        events.push_back(std::string("erase:") + key);
        if (erase_result != ConfigResult::kOk) {
            return erase_result;
        }
        return values.erase(key) == 0 ? ConfigResult::kNotFound : ConfigResult::kOk;
    }

    std::map<std::string, std::string> values;
    std::vector<std::string> events;
    ConfigResult read_result = ConfigResult::kOk;
    ConfigResult write_result = ConfigResult::kOk;
    ConfigResult erase_result = ConfigResult::kOk;
};

class MemoryWifi final : public SavedWifiPort {
public:
    ConfigResult LoadSaved(std::vector<WifiCredential>* output) override {
        if (result == ConfigResult::kOk) {
            *output = credentials;
        }
        return result;
    }

    ConfigResult Save(const WifiCredential& credential) override {
        if (save_result == ConfigResult::kOk) credentials = {credential};
        return save_result;
    }

    std::vector<WifiCredential> credentials;
    ConfigResult result = ConfigResult::kOk;
    ConfigResult save_result = ConfigResult::kOk;
};

void TestEndpointParsingAndBounds() {
    EndpointSnapshot endpoint;
    assert(DeviceConfig::ParseEndpoint("wss://Voice.Example:8443/v1/voice?mode=udp", &endpoint) == ConfigResult::kOk);
    assert(endpoint.secure);
    assert(endpoint.host == "voice.example");
    assert(endpoint.port == 8443);
    assert(endpoint.origin == "wss://voice.example:8443");

    assert(DeviceConfig::ParseEndpoint("ws://127.0.0.1/v1/voice", &endpoint) == ConfigResult::kOk);
    assert(!endpoint.secure && endpoint.port == 80);
    assert(endpoint.origin == "ws://127.0.0.1:80");
    assert(DeviceConfig::ParseEndpoint("https://director.example/v1/session/bootstrap", &endpoint) ==
           ConfigResult::kOk);
    assert(endpoint.secure && endpoint.origin == "https://director.example:443");
    assert(DeviceConfig::ParseEndpoint("wss://[2001:db8::1]/voice", &endpoint) == ConfigResult::kOk);
    assert(endpoint.host == "[2001:db8::1]" && endpoint.port == 443);

    const std::string maximum = "ws://a/" + std::string(rva::config::kMaxWebsocketUrlBytes - 7, 'x');
    assert(maximum.size() == rva::config::kMaxWebsocketUrlBytes);
    assert(DeviceConfig::ParseEndpoint(maximum, &endpoint) == ConfigResult::kOk);
    assert(DeviceConfig::ParseEndpoint(maximum + "x", &endpoint) == ConfigResult::kInvalidArgument);
    assert(DeviceConfig::ParseEndpoint("ftp://voice.example/v1/voice", &endpoint) == ConfigResult::kInvalidArgument);
    assert(DeviceConfig::ParseEndpoint("ws:///v1/voice", &endpoint) == ConfigResult::kInvalidArgument);
    assert(DeviceConfig::ParseEndpoint("ws://:8080/v1/voice", &endpoint) == ConfigResult::kInvalidArgument);
    assert(DeviceConfig::ParseEndpoint("ws://voice.example:/v1/voice", &endpoint) == ConfigResult::kInvalidArgument);
    assert(DeviceConfig::ParseEndpoint("ws://user@voice.example/v1/voice", &endpoint) ==
           ConfigResult::kInvalidArgument);
    assert(DeviceConfig::ParseEndpoint("ws://voice.example/v1/voice#fragment", &endpoint) ==
           ConfigResult::kInvalidArgument);
    assert(DeviceConfig::ParseEndpoint("ws://voice%2eexample/v1/voice", &endpoint) ==
           ConfigResult::kInvalidArgument);
    assert(DeviceConfig::ParseEndpoint("ws://voice\\example/v1/voice", &endpoint) ==
           ConfigResult::kInvalidArgument);
}

void TestEndpointSelectionBindsTokenToExactOrigin() {
    MemoryStore store;
    MemoryWifi wifi;
    DeviceConfig config(store, wifi);
    store.values[rva::config::kEndpointUrlKey] = "wss://voice.example/v1/voice";
    store.values[rva::config::kEndpointTokenKey] = "stored-token";
    store.values[rva::config::kEndpointTokenOriginKey] = "wss://other.example:443";

    EndpointSnapshot endpoint;
    assert(config.ResolveEndpoint({}, &endpoint) == ConfigResult::kOk);
    assert(endpoint.source == EndpointSource::kSaved);
    assert(endpoint.token.empty());

    store.values[rva::config::kEndpointTokenOriginKey] = "wss://voice.example:443";
    assert(config.ResolveEndpoint({}, &endpoint) == ConfigResult::kOk);
    assert(endpoint.token == "stored-token");

    store.values.erase(rva::config::kEndpointTokenKey);
    store.values.erase(rva::config::kEndpointTokenOriginKey);
    const std::vector<EndpointCandidate> same_origin = {
        {"wss://voice.example/another-path", "provisioned-token", "wss://voice.example:443"},
    };
    assert(config.ResolveEndpoint(same_origin, &endpoint) == ConfigResult::kOk);
    assert(endpoint.source == EndpointSource::kSaved);
    assert(endpoint.token == "provisioned-token");

    store.values[rva::config::kEndpointUrlKey] = "invalid";
    const std::vector<EndpointCandidate> provisioned = {
        {"ftp://invalid", "ignored", "ftp://invalid:21"},
        {"ws://fallback.example/v1/voice", "fallback-token", "ws://fallback.example:80"},
    };
    assert(config.ResolveEndpoint(provisioned, &endpoint) == ConfigResult::kOk);
    assert(endpoint.source == EndpointSource::kProvisioned);
    assert(endpoint.host == "fallback.example");
    assert(endpoint.token == "fallback-token");
}

void TestEndpointOriginChangeClearsTokenBeforeSavingUrl() {
    MemoryStore store;
    MemoryWifi wifi;
    DeviceConfig config(store, wifi);
    store.values[rva::config::kEndpointUrlKey] = "wss://old.example/v1/voice";
    store.values[rva::config::kEndpointTokenKey] = "old-token";
    store.values[rva::config::kEndpointTokenOriginKey] = "wss://old.example:443";

    assert(config.SaveEndpoint("wss://new.example/v1/voice") == ConfigResult::kOk);
    assert(store.values.count(rva::config::kEndpointTokenKey) == 0);
    assert(store.values.count(rva::config::kEndpointTokenOriginKey) == 0);
    assert(store.values[rva::config::kEndpointUrlKey] == "wss://new.example/v1/voice");
    assert((store.events == std::vector<std::string>{
        "erase:token", "erase:token_origin", "write:ws_url"}));

    store.events.clear();
    assert(config.BindToken("wss://new.example/v1/voice", "new-token") == ConfigResult::kOk);
    assert((store.events == std::vector<std::string>{
        "erase:token", "write:token_origin", "write:token"}));
    assert(store.values[rva::config::kEndpointTokenOriginKey] == "wss://new.example:443");

    store.events.clear();
    assert(config.SaveEndpoint("wss://new.example/another-path") == ConfigResult::kOk);
    assert((store.events == std::vector<std::string>{"write:ws_url"}));
    assert(store.values[rva::config::kEndpointTokenKey] == "new-token");
}

void TestUnboundLegacyTokenIsCleared() {
    MemoryStore store;
    MemoryWifi wifi;
    DeviceConfig config(store, wifi);
    store.values[rva::config::kEndpointTokenKey] = "legacy-token";

    assert(config.SaveEndpoint("wss://voice.example/v1/voice") == ConfigResult::kOk);
    assert(store.values.count(rva::config::kEndpointTokenKey) == 0);
}

void TestEndpointChangeDoesNotCommitIfCredentialCleanupFails() {
    MemoryStore store;
    MemoryWifi wifi;
    DeviceConfig config(store, wifi);
    store.values[rva::config::kEndpointUrlKey] = "wss://old.example/v1/voice";
    store.values[rva::config::kEndpointTokenKey] = "old-token";
    store.values[rva::config::kEndpointTokenOriginKey] = "wss://old.example:443";
    store.erase_result = ConfigResult::kStorageFailure;

    assert(config.SaveEndpoint("wss://new.example/v1/voice") == ConfigResult::kStorageFailure);
    assert(store.values[rva::config::kEndpointUrlKey] == "wss://old.example/v1/voice");
    const std::vector<std::string> expected_events = {"erase:token"};
    assert(store.events == expected_events);
}

void TestWifiPlanPrioritizesSavedCredentialsAndIsASnapshot() {
    MemoryStore store;
    MemoryWifi wifi;
    wifi.credentials = {{"saved-primary", "saved-password"}, {"duplicate", "saved-version"}};
    DeviceConfig config(store, wifi);
    std::vector<WifiCredential> provisioned = {
        {"duplicate", "provisioned-version"},
        {"fallback", "fallback-password"},
    };

    WifiPlan plan;
    assert(config.LoadWifiPlan(provisioned, &plan) == ConfigResult::kOk);
    const std::vector<WifiCredential> expected = {
        {"saved-primary", "saved-password"},
        {"duplicate", "saved-version"},
        {"fallback", "fallback-password"},
    };
    assert(plan.credentials() == expected);

    wifi.credentials.clear();
    provisioned.clear();
    assert(plan.credentials().size() == 3);
    assert(plan.credentials().front().ssid == "saved-primary");
}

void TestNvsPhysicalKeysFitEspIdfLimit() {
    assert(std::string(rva::config::kNvsNamespace).size() <= rva::config::kMaxNvsPhysicalKeyBytes);
    assert(std::string(rva::config::kEndpointUrlKey).size() <= rva::config::kMaxNvsPhysicalKeyBytes);
    assert(std::string(rva::config::kEndpointTokenKey).size() <= rva::config::kMaxNvsPhysicalKeyBytes);
    assert(std::string(rva::config::kEndpointTokenOriginKey).size() <= rva::config::kMaxNvsPhysicalKeyBytes);
}

void TestWifiSaveValidationAndPersistence() {
    MemoryStore store;
    MemoryWifi wifi;
    DeviceConfig config(store, wifi);

    assert(config.SaveWifi({"", "password"}) == ConfigResult::kInvalidArgument);
    assert(config.SaveWifi({"network", "short"}) == ConfigResult::kInvalidArgument);
    assert(config.SaveWifi({std::string(33, 's'), "password"}) == ConfigResult::kInvalidArgument);
    assert(config.SaveWifi({"open-network", ""}) == ConfigResult::kOk);
    assert(wifi.credentials.size() == 1 && wifi.credentials.front().ssid == "open-network" &&
           wifi.credentials.front().password.empty());
    assert(config.SaveWifi({"secure-network", "password"}) == ConfigResult::kOk);
    assert(wifi.credentials.size() == 1 && wifi.credentials.front().ssid == "secure-network" &&
           wifi.credentials.front().password == "password");
}

}  // namespace

int main() {
    TestEndpointParsingAndBounds();
    TestEndpointSelectionBindsTokenToExactOrigin();
    TestEndpointOriginChangeClearsTokenBeforeSavingUrl();
    TestUnboundLegacyTokenIsCleared();
    TestEndpointChangeDoesNotCommitIfCredentialCleanupFails();
    TestWifiPlanPrioritizesSavedCredentialsAndIsASnapshot();
    TestNvsPhysicalKeysFitEspIdfLimit();
    TestWifiSaveValidationAndPersistence();
    return 0;
}
