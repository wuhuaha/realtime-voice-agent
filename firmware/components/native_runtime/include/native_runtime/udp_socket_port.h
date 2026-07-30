#pragma once

#include <atomic>
#include <cstdint>
#include <string>

#include "transport_udp/udp_runtime.h"

namespace rva::runtime {

class UdpSocketPort final : public udp::DatagramIoPort {
public:
    ~UdpSocketPort() override;

    bool Open(const std::string& host, uint16_t port, udp::Endpoint* endpoint);
    udp::DatagramSendOutcome Send(
        const udp::Endpoint& destination, const uint8_t* data, size_t size) override;
    bool Receive(uint8_t* data, size_t capacity, size_t* size,
                 udp::Endpoint* source, uint32_t timeout_ms) override;
    void Interrupt() override;
    void Close() override;

private:
    std::atomic<int> socket_{-1};
    uint32_t generation_ = 0;
    uint16_t local_source_port_ = 0;
};

}  // namespace rva::runtime
