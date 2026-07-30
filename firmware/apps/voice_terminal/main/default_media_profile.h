#pragma once

#include <cstdint>

namespace rva::app {

enum class DefaultMediaProfile : uint8_t {
    kWss,
    kUdp,
};

constexpr DefaultMediaProfile ConfiguredDefaultMediaProfile() noexcept {
#if defined(CONFIG_RVA_DEFAULT_MEDIA_PROFILE_WSS) && \
    CONFIG_RVA_DEFAULT_MEDIA_PROFILE_WSS
    return DefaultMediaProfile::kWss;
#elif defined(CONFIG_RVA_DEFAULT_MEDIA_PROFILE_UDP) && \
    CONFIG_RVA_DEFAULT_MEDIA_PROFILE_UDP
    return DefaultMediaProfile::kUdp;
#else
    return DefaultMediaProfile::kWss;
#endif
}

}  // namespace rva::app
