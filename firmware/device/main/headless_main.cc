#include "voice_contracts/udp_wire.h"
#include "voice_contracts/transport_profile.h"
#include "voice_core/session.h"
#include "voice_core/session_gate.h"

#include <algorithm>
#include <array>
#include <cassert>
#include <string_view>

#include <esp_log.h>

namespace {

constexpr char kTag[] = "HeadlessContract";

class SmokePorts final : public voice::core::TransportPort,
                         public voice::core::AudioPort,
                         public voice::core::EventSink {
public:
    bool SendControl(
        const voice::core::SessionSnapshot&, std::string_view message) noexcept override {
        return !message.empty();
    }
    void SetPlaybackGeneration(const voice::core::SessionSnapshot&) noexcept override {}
    void Revoke(const voice::core::SessionSnapshot&) noexcept override {}
    void OnSessionEvent(
        voice::core::SessionEvent, const voice::core::SessionSnapshot&) noexcept override {}
};

}  // namespace

extern "C" void app_main() {
    voice::core::SessionGate gate;
    assert(gate.BeginFreshSession(1));
    assert(gate.CommitMedia(
        voice::contracts::TransportProfile::kWssOpusV1,
        voice::core::MediaOwner::kWss));
    assert(gate.AdvancePlaybackGeneration(1));
    assert(gate.BeginClose());
    assert(gate.FinishClose());

    SmokePorts ports;
    voice::core::Session session(ports, ports, ports);
    assert(session.BeginFreshSession(1));
    const auto owner = session.CommitHello(
        1, voice::contracts::TransportProfile::kUdpOpusGcmV1,
        voice::core::MediaOwner::kUdp);
    assert(owner);
    assert(session.SendControl(*owner, "{}"));
    assert(session.RequestClose(*owner));
    assert(session.FinishClose(1));

    namespace udp = voice::contracts::udp_v1;
    udp::Header header;
    header.type = udp::DatagramType::kKeepalive;
    header.media_epoch = 1;
    const auto encoded = udp::EncodeHeader(header);
    std::array<std::uint8_t, udp::kHeaderBytes + udp::kTagBytes> datagram{};
    std::copy(encoded.begin(), encoded.end(), datagram.begin());
    assert(udp::ParseDatagram(
        datagram.data(), datagram.size(), udp::Direction::kUplink));
    ESP_LOGI(kTag, "transport-neutral session contract smoke passed");
}
