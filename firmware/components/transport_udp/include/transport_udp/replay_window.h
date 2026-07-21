#pragma once

#include <cstdint>

namespace rva::udp {

class ReplayWindow final {
public:
    static constexpr uint32_t kMaxForwardDistance = 1024;
    bool CanAccept(uint32_t sequence) const;
    void Commit(uint32_t sequence);
    void Reset();
private:
    uint32_t highest_ = 0;
    uint64_t bitmap_ = 0;
};

}  // namespace rva::udp
