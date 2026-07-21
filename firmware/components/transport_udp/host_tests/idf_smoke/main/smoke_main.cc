#include "transport_udp/gcm_crypto.h"
#include "transport_udp/udp_session.h"

extern "C" void app_main() {
    rva::udp::MbedTlsGcm uplink;
    rva::udp::MbedTlsGcm downlink;
    rva::udp::UdpSession session(uplink, downlink);
}
