#pragma once

#include <cstdint>
#include <string>

namespace rva::runtime {

struct BootstrapGrant final {
    std::string worker_id;
    std::string worker_wss_url;
    std::string connect_grant;
    std::string session_epoch;
    uint64_t fencing_token = 0;

    bool HasReleaseIdentity() const noexcept;
};

class DirectorBootstrap final {
public:
    bool Request(
        const std::string& url,
        const std::string& authorization_token,
        const std::string& tenant_id,
        const std::string& device_id,
        BootstrapGrant* grant) noexcept;

    bool Release(
        const std::string& bootstrap_url,
        const std::string& authorization_token,
        const std::string& tenant_id,
        const std::string& device_id,
        const BootstrapGrant& grant) noexcept;
};

}  // namespace rva::runtime
