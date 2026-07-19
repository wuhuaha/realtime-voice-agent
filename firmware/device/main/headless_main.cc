#include "voice_contracts/transport_profile.h"
#include "voice_core/session_gate.h"

#include <cassert>

#include <esp_log.h>

namespace {

constexpr char kTag[] = "HeadlessContract";

}  // namespace

extern "C" void app_main() {
    voice::core::SessionGate gate;
    assert(gate.BeginFreshSession(1));
    assert(gate.CommitMedia(
        voice::contracts::TransportProfile::kWssOpusV1,
        voice::core::MediaOwner::kWss));
    assert(gate.AdvancePlaybackGeneration(2));
    assert(gate.BeginClose());
    assert(gate.FinishClose());
    ESP_LOGI(kTag, "transport-neutral session contract smoke passed");
}
