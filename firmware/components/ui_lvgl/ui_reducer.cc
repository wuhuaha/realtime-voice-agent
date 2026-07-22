#include "ui_lvgl/ui_state.h"

#include <algorithm>

namespace rva::ui {
namespace {

template <typename T>
T EnumValue(uint32_t value, T fallback, uint32_t upper_bound) {
    return value < upper_bound ? static_cast<T>(value) : fallback;
}

void AppendBounded(std::string* destination, const std::string& text) {
    destination->append(text);
    *destination = TruncateUtf8(std::move(*destination), kMaxTranscriptBytes);
}

}  // namespace

std::optional<UiEvent> Reduce(UiState* state, const UiCommand& command) {
    if (state == nullptr) {
        return std::nullopt;
    }
    switch (command.kind) {
        case CommandKind::kSetConnection:
            state->connection = EnumValue(
                command.value, state->connection, static_cast<uint32_t>(ConnectionState::kError) + 1);
            break;
        case CommandKind::kSetConversation:
            state->conversation = EnumValue(
                command.value, state->conversation,
                static_cast<uint32_t>(ConversationState::kSpeaking) + 1);
            break;
        case CommandKind::kSetPreferredTransport:
            state->preferred_transport = EnumValue(
                command.value, state->preferred_transport, static_cast<uint32_t>(Transport::kUdp) + 1);
            break;
        case CommandKind::kSetActiveTransport:
            state->active_transport = EnumValue(
                command.value, state->active_transport, static_cast<uint32_t>(Transport::kUdp) + 1);
            break;
        case CommandKind::kSetAsrText:
            state->asr_text = TruncateUtf8(command.text, kMaxTranscriptBytes);
            break;
        case CommandKind::kAppendAsrText:
            AppendBounded(&state->asr_text, command.text);
            break;
        case CommandKind::kSetResponseText:
            state->response_text = TruncateUtf8(command.text, kMaxTranscriptBytes);
            break;
        case CommandKind::kAppendResponseText:
            AppendBounded(&state->response_text, command.text);
            break;
        case CommandKind::kClearTranscript:
            state->asr_text.clear();
            state->response_text.clear();
            break;
        case CommandKind::kOpenWifi:
            state->page = Page::kWifi;
            break;
        case CommandKind::kClearWifiNetworks:
            state->wifi_networks.clear();
            break;
        case CommandKind::kAddWifiNetwork: {
            WifiNetwork network{
                .ssid = TruncateUtf8(command.text, 32),
                .rssi = static_cast<int8_t>(command.value & 0xffU),
                .secured = (command.value & 0x100U) != 0,
            };
            if (!network.ssid.empty()) {
                const auto found = std::find_if(
                    state->wifi_networks.begin(), state->wifi_networks.end(),
                    [&network](const WifiNetwork& value) { return value.ssid == network.ssid; });
                if (found == state->wifi_networks.end() && state->wifi_networks.size() < 20) {
                    state->wifi_networks.push_back(std::move(network));
                }
            }
            break;
        }
        case CommandKind::kOpenWifiKeyboard:
            state->selected_ssid = TruncateUtf8(command.text, 32);
            state->page = Page::kWifiKeyboard;
            break;
        case CommandKind::kOpenEndpoint:
            state->endpoint_draft = TruncateUtf8(command.text, 255);
            state->page = Page::kEndpoint;
            break;
        case CommandKind::kSetEndpointDraft:
            state->endpoint_draft = TruncateUtf8(command.text, 255);
            break;
        case CommandKind::kSetConfigMessage:
            state->config_message = TruncateUtf8(command.text, 96);
            break;
        case CommandKind::kBackHome:
            state->page = Page::kHome;
            state->selected_ssid.clear();
            state->config_message.clear();
            break;
        case CommandKind::kSettingsPressed:
            state->page = Page::kWifi;
            state->config_message.clear();
            return UiEvent{.kind = EventKind::kRequestWifiScan, .transport = Transport::kWss, .text = {}, .secret = {}};
        case CommandKind::kMicPressed:
            if (state->conversation != ConversationState::kIdle) {
                state->conversation = ConversationState::kIdle;
                return UiEvent{.kind = EventKind::kStopConversation,
                               .transport = state->active_transport,
                               .text = {}, .secret = {}};
            }
            state->conversation = ConversationState::kConnecting;
            return UiEvent{.kind = EventKind::kStartConversation,
                           .transport = state->preferred_transport,
                           .text = {}, .secret = {}};
        case CommandKind::kTransportPressed:
            if (state->conversation == ConversationState::kIdle) {
                state->preferred_transport = state->preferred_transport == Transport::kWss
                                                 ? Transport::kUdp
                                                 : Transport::kWss;
                return UiEvent{
                    .kind = EventKind::kSelectTransport,
                    .transport = state->preferred_transport,
                    .text = {},
                    .secret = {},
                };
            }
            break;
    }
    return std::nullopt;
}

std::string TruncateUtf8(std::string text, size_t max_bytes) {
    if (text.size() <= max_bytes) {
        return text;
    }
    size_t cut = max_bytes;
    while (cut > 0 && (static_cast<unsigned char>(text[cut]) & 0xC0U) == 0x80U) {
        --cut;
    }
    text.resize(cut);
    return text;
}

const char* ConnectionLabel(ConnectionState state) {
    switch (state) {
        case ConnectionState::kOffline: return "离线";
        case ConnectionState::kConnecting: return "连接中";
        case ConnectionState::kOnline: return "在线";
        case ConnectionState::kError: return "连接异常";
    }
    return "离线";
}

const char* ConversationLabel(ConversationState state) {
    switch (state) {
        case ConversationState::kIdle: return "待机";
        case ConversationState::kConnecting: return "连接中...";
        case ConversationState::kListening: return "聆听中...";
        case ConversationState::kThinking: return "思考中...";
        case ConversationState::kSpeaking: return "回复中...";
    }
    return "待机";
}

const char* TransportLabel(Transport transport) {
    return transport == Transport::kUdp ? "UDP" : "WSS";
}

}  // namespace rva::ui
