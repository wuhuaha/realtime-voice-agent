#include <cassert>

#include "default_media_profile.h"

int main() {
    constexpr auto configured = rva::app::ConfiguredDefaultMediaProfile();

#if defined(RVA_EXPECT_UDP) && RVA_EXPECT_UDP
    static_assert(configured == rva::app::DefaultMediaProfile::kUdp);
    assert(configured == rva::app::DefaultMediaProfile::kUdp);
#else
    static_assert(configured == rva::app::DefaultMediaProfile::kWss);
    assert(configured == rva::app::DefaultMediaProfile::kWss);
#endif

    return 0;
}
