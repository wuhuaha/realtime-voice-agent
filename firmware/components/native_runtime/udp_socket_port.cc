#include "native_runtime/udp_socket_port.h"

#include <algorithm>
#include <cstring>
#include <limits>

#include <lwip/netdb.h>
#include <lwip/sockets.h>

namespace rva::runtime {
namespace {

bool ToEndpoint(const sockaddr* address, socklen_t size, udp::Endpoint* endpoint) {
    if (address == nullptr || endpoint == nullptr) return false;
    udp::Endpoint parsed;
    if (address->sa_family == AF_INET && size >= sizeof(sockaddr_in)) {
        const auto* ipv4 = reinterpret_cast<const sockaddr_in*>(address);
        parsed.address_bytes = 4;
        parsed.port = ntohs(ipv4->sin_port);
        std::memcpy(parsed.address.data(), &ipv4->sin_addr, parsed.address_bytes);
    } else if (address->sa_family == AF_INET6 && size >= sizeof(sockaddr_in6)) {
        const auto* ipv6 = reinterpret_cast<const sockaddr_in6*>(address);
        parsed.address_bytes = 16;
        parsed.port = ntohs(ipv6->sin6_port);
        std::memcpy(parsed.address.data(), &ipv6->sin6_addr, parsed.address_bytes);
    } else {
        return false;
    }
    if (!parsed.valid()) return false;
    *endpoint = parsed;
    return true;
}

bool ToSockaddr(const udp::Endpoint& endpoint, sockaddr_storage* storage, socklen_t* size) {
    if (!endpoint.valid() || storage == nullptr || size == nullptr) return false;
    std::memset(storage, 0, sizeof(*storage));
    if (endpoint.address_bytes == 4) {
        auto* ipv4 = reinterpret_cast<sockaddr_in*>(storage);
        ipv4->sin_family = AF_INET;
        ipv4->sin_port = htons(endpoint.port);
        std::memcpy(&ipv4->sin_addr, endpoint.address.data(), 4);
        *size = sizeof(*ipv4);
        return true;
    }
    auto* ipv6 = reinterpret_cast<sockaddr_in6*>(storage);
    ipv6->sin6_family = AF_INET6;
    ipv6->sin6_port = htons(endpoint.port);
    std::memcpy(&ipv6->sin6_addr, endpoint.address.data(), 16);
    *size = sizeof(*ipv6);
    return true;
}

}  // namespace

UdpSocketPort::~UdpSocketPort() {
    Close();
}

bool UdpSocketPort::Open(const std::string& host, uint16_t port, udp::Endpoint* endpoint) {
    if (socket_.load() >= 0 || host.empty() || port == 0 || endpoint == nullptr) return false;
    addrinfo hints{};
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_DGRAM;
    hints.ai_protocol = IPPROTO_UDP;
    addrinfo* addresses = nullptr;
    const std::string service = std::to_string(port);
    if (getaddrinfo(host.c_str(), service.c_str(), &hints, &addresses) != 0) return false;
    bool opened = false;
    for (const addrinfo* candidate = addresses; candidate != nullptr; candidate = candidate->ai_next) {
        udp::Endpoint resolved;
        if (!ToEndpoint(candidate->ai_addr, candidate->ai_addrlen, &resolved)) continue;
        const int handle = socket(candidate->ai_family, SOCK_DGRAM, IPPROTO_UDP);
        if (handle < 0) continue;
        socket_.store(handle);
        *endpoint = resolved;
        opened = true;
        break;
    }
    freeaddrinfo(addresses);
    return opened;
}

int UdpSocketPort::Send(const udp::Endpoint& destination, const uint8_t* data, size_t size) {
    const int handle = socket_.load();
    if (handle < 0 || data == nullptr || size == 0 ||
        size > static_cast<size_t>(std::numeric_limits<int>::max())) return -1;
    sockaddr_storage address{};
    socklen_t address_size = 0;
    if (!ToSockaddr(destination, &address, &address_size)) return -1;
    return sendto(handle, data, size, 0, reinterpret_cast<sockaddr*>(&address), address_size);
}

bool UdpSocketPort::Receive(uint8_t* data, size_t capacity, size_t* size,
                            udp::Endpoint* source, uint32_t timeout_ms) {
    const int handle = socket_.load();
    if (handle < 0 || data == nullptr || capacity == 0 || size == nullptr || source == nullptr) return false;
    fd_set read_set;
    FD_ZERO(&read_set);
    FD_SET(handle, &read_set);
    timeval timeout{
        .tv_sec = static_cast<long>(timeout_ms / 1000),
        .tv_usec = static_cast<long>((timeout_ms % 1000) * 1000),
    };
    if (select(handle + 1, &read_set, nullptr, nullptr, &timeout) <= 0) return false;
    sockaddr_storage address{};
    socklen_t address_size = sizeof(address);
    const int received = recvfrom(
        handle, data, capacity, 0, reinterpret_cast<sockaddr*>(&address), &address_size);
    if (received <= 0 || !ToEndpoint(reinterpret_cast<sockaddr*>(&address), address_size, source)) return false;
    *size = static_cast<size_t>(received);
    return true;
}

void UdpSocketPort::Interrupt() {
    const int handle = socket_.load();
    if (handle >= 0) shutdown(handle, SHUT_RDWR);
}

void UdpSocketPort::Close() {
    const int handle = socket_.exchange(-1);
    if (handle >= 0) close(handle);
}

}  // namespace rva::runtime
