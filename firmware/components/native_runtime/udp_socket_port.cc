#include "native_runtime/udp_socket_port.h"

#include <algorithm>
#include <cerrno>
#include <cstring>
#include <limits>

#include <esp_log.h>
#include <lwip/netdb.h>
#include <lwip/sockets.h>

#include "transport_udp/endpoint_diagnostics.h"

namespace rva::runtime {
namespace {

constexpr char kLogTag[] = "rva-udp-socket";
std::atomic<uint32_t> g_socket_generation{0};

uint32_t NextSocketGeneration() {
    uint32_t generation = g_socket_generation.fetch_add(1) + 1;
    if (generation == 0) generation = g_socket_generation.fetch_add(1) + 1;
    return generation;
}

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

bool BindEphemeral(int handle, int family, uint16_t* local_source_port) {
    if (handle < 0 || local_source_port == nullptr) return false;
    sockaddr_storage storage{};
    socklen_t size = 0;
    if (family == AF_INET) {
        auto* ipv4 = reinterpret_cast<sockaddr_in*>(&storage);
        ipv4->sin_family = AF_INET;
        ipv4->sin_port = htons(0);
        ipv4->sin_addr.s_addr = htonl(INADDR_ANY);
        size = sizeof(*ipv4);
    } else if (family == AF_INET6) {
        auto* ipv6 = reinterpret_cast<sockaddr_in6*>(&storage);
        ipv6->sin6_family = AF_INET6;
        ipv6->sin6_port = htons(0);
        size = sizeof(*ipv6);
    } else {
        return false;
    }
    if (bind(handle, reinterpret_cast<sockaddr*>(&storage), size) != 0) return false;
    size = sizeof(storage);
    if (getsockname(handle, reinterpret_cast<sockaddr*>(&storage), &size) != 0) return false;
    uint16_t port = 0;
    if (storage.ss_family == AF_INET && size >= sizeof(sockaddr_in)) {
        port = ntohs(reinterpret_cast<const sockaddr_in*>(&storage)->sin_port);
    } else if (storage.ss_family == AF_INET6 && size >= sizeof(sockaddr_in6)) {
        port = ntohs(reinterpret_cast<const sockaddr_in6*>(&storage)->sin6_port);
    }
    if (port == 0) return false;
    *local_source_port = port;
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
        uint16_t local_source_port = 0;
        if (!BindEphemeral(handle, candidate->ai_family, &local_source_port)) {
            close(handle);
            continue;
        }
        socket_.store(handle);
        generation_ = NextSocketGeneration();
        local_source_port_ = local_source_port;
        char peer_address[40]{};
        if (!udp::FormatEndpointAddressForLog(resolved, peer_address, sizeof(peer_address))) {
            close(socket_.exchange(-1));
            generation_ = 0;
            local_source_port_ = 0;
            continue;
        }
        *endpoint = resolved;
        ESP_LOGI(kLogTag,
                 "udp_socket_opened peer_address=%s peer_port=%u local_source_port=%u socket_generation=%lu",
                 peer_address, static_cast<unsigned>(resolved.port),
                 static_cast<unsigned>(local_source_port_),
                 static_cast<unsigned long>(generation_));
        opened = true;
        break;
    }
    freeaddrinfo(addresses);
    return opened;
}

udp::DatagramSendOutcome UdpSocketPort::Send(
    const udp::Endpoint& destination, const uint8_t* data, size_t size) {
    udp::DatagramSendOutcome outcome{
        .sent_bytes = -1,
        .error_code = 0,
        .socket_generation = generation_,
        .local_source_port = local_source_port_,
    };
    const int handle = socket_.load();
    if (handle < 0) {
        outcome.error_code = EBADF;
        return outcome;
    }
    if (data == nullptr || size == 0) {
        outcome.error_code = EINVAL;
        return outcome;
    }
    if (size > static_cast<size_t>(std::numeric_limits<int>::max())) {
        outcome.error_code = EMSGSIZE;
        return outcome;
    }
    sockaddr_storage address{};
    socklen_t address_size = 0;
    if (!ToSockaddr(destination, &address, &address_size)) {
        outcome.error_code = EINVAL;
        return outcome;
    }
    outcome.sent_bytes = sendto(
        handle, data, size, 0, reinterpret_cast<sockaddr*>(&address), address_size);
    if (outcome.sent_bytes < 0) outcome.error_code = errno;
    return outcome;
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
