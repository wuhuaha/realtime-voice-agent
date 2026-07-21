#pragma once

#include <string>

namespace rva::runtime {

struct BootstrapGrant final {
    std::string worker_wss_url;
    std::string connect_grant;
    std::string session_epoch;
};

class DirectorBootstrap final {
public:
    bool Request(
        const std::string& url,
        const std::string& authorization_token,
        const std::string& tenant_id,
        const std::string& device_id,
        BootstrapGrant* grant);
};

}  // namespace rva::runtime
