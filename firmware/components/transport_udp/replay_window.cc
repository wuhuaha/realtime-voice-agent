#include "transport_udp/replay_window.h"

namespace rva::udp {

bool ReplayWindow::CanAccept(uint32_t sequence) const {
    if (sequence > highest_) return sequence - highest_ <= kMaxForwardDistance;
    const uint32_t distance = highest_ - sequence;
    return distance < 64 && (bitmap_ & (uint64_t{1} << distance)) == 0;
}

void ReplayWindow::Commit(uint32_t sequence) {
    if (sequence > highest_) {
        const uint32_t shift = sequence - highest_;
        bitmap_ = shift >= 64 ? 1 : (bitmap_ << shift) | 1;
        highest_ = sequence;
    } else {
        bitmap_ |= uint64_t{1} << (highest_ - sequence);
    }
}

void ReplayWindow::Reset() { highest_ = 0; bitmap_ = 0; }

}  // namespace rva::udp
