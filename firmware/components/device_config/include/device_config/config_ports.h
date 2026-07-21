#pragma once

#include <string>
#include <vector>

namespace rva::config {

enum class ConfigResult {
    kOk = 0,
    kNotFound,
    kInvalidArgument,
    kStorageFailure,
};

struct WifiCredential final {
    std::string ssid;
    std::string password;

    bool operator==(const WifiCredential& other) const {
        return ssid == other.ssid && password == other.password;
    }
};

class ConfigStorePort {
public:
    virtual ~ConfigStorePort() = default;
    virtual ConfigResult ReadString(const char* key, std::string* value) = 0;
    virtual ConfigResult WriteString(const char* key, const std::string& value) = 0;
    virtual ConfigResult Erase(const char* key) = 0;
};

class SavedWifiPort {
public:
    virtual ~SavedWifiPort() = default;
    virtual ConfigResult LoadSaved(std::vector<WifiCredential>* credentials) = 0;
    virtual ConfigResult Save(const WifiCredential& credential) = 0;
};

}  // namespace rva::config
