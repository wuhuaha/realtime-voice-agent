#include <cassert>
#include <string>

#include "ui_lvgl/ui_lifecycle.h"
#include "ui_lvgl/ui_state.h"

using namespace rva::ui;

int main() {
    UiLifecycle lifecycle;
    assert(lifecycle.Begin());
    assert(lifecycle.active());
    assert(lifecycle.Begin());
    lifecycle.End();
    assert(!lifecycle.active());
    assert(lifecycle.consumed());
    assert(!lifecycle.Begin());

    UiState state;
    state.connection = ConnectionState::kOnline;

    auto event = Reduce(&state, {.kind = CommandKind::kMicPressed, .value = 0, .text = {}});
    assert(event.has_value() && event->kind == EventKind::kStartConversation);
    assert(state.conversation == ConversationState::kConnecting);

    event = Reduce(&state, {.kind = CommandKind::kMicPressed, .value = 0, .text = {}});
    assert(event.has_value() && event->kind == EventKind::kStopConversation);
    assert(state.conversation == ConversationState::kIdle);

    state.connection = ConnectionState::kOffline;
    event = Reduce(&state, {.kind = CommandKind::kMicPressed, .value = 0, .text = {}});
    assert(event.has_value() && event->kind == EventKind::kStartConversation);
    assert(state.conversation == ConversationState::kConnecting);

    state.conversation = ConversationState::kListening;
    event = Reduce(&state, {.kind = CommandKind::kMicPressed, .value = 0, .text = {}});
    assert(event.has_value() && event->kind == EventKind::kStopConversation);
    assert(state.conversation == ConversationState::kIdle);

    state.conversation = ConversationState::kListening;
    event = Reduce(&state, {.kind = CommandKind::kTransportPressed, .value = 0, .text = {}});
    assert(!event.has_value());
    assert(state.preferred_transport == Transport::kUdp);

    state.conversation = ConversationState::kIdle;
    event = Reduce(&state, {.kind = CommandKind::kTransportPressed, .value = 0, .text = {}});
    assert(event.has_value() && event->kind == EventKind::kSelectTransport);
    assert(event->transport == Transport::kWss);

    const std::string chinese = "中文语音";
    assert(TruncateUtf8(chinese, 4) == "中");

    Reduce(&state, {.kind = CommandKind::kAppendAsrText, .value = 0,
                    .text = std::string(kMaxTranscriptBytes, 'a') + chinese});
    assert(state.asr_text.size() <= kMaxTranscriptBytes);

    event = Reduce(&state, {.kind = CommandKind::kSettingsPressed, .value = 0, .text = {}});
    assert(event.has_value() && event->kind == EventKind::kRequestWifiScan);
    assert(state.page == Page::kWifi);
    Reduce(&state, {.kind = CommandKind::kAddWifiNetwork,
                    .value = static_cast<uint8_t>(-42) | 0x100U,
                    .text = "office"});
    Reduce(&state, {.kind = CommandKind::kAddWifiNetwork,
                    .value = static_cast<uint8_t>(-70),
                    .text = "guest"});
    Reduce(&state, {.kind = CommandKind::kAddWifiNetwork,
                    .value = static_cast<uint8_t>(-20),
                    .text = "office"});
    assert(state.wifi_networks.size() == 2);
    assert(state.wifi_networks.front().ssid == "office");
    assert(state.wifi_networks.front().secured);
    assert(state.wifi_networks.back().ssid == "guest");
    assert(!state.wifi_networks.back().secured);
    return 0;
}
