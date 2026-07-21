#pragma once

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

namespace rva::ui {

enum class ConnectionState : uint8_t { kOffline, kConnecting, kOnline, kError };
enum class ConversationState : uint8_t { kIdle, kListening, kThinking, kSpeaking };
enum class Transport : uint8_t { kWss, kUdp };
enum class Page : uint8_t { kHome, kWifi, kWifiKeyboard, kEndpoint };

struct WifiNetwork final {
    std::string ssid;
    int8_t rssi = -127;
    bool secured = true;
};

struct UiState final {
    ConnectionState connection = ConnectionState::kOffline;
    ConversationState conversation = ConversationState::kIdle;
    Transport preferred_transport = Transport::kWss;
    Transport active_transport = Transport::kWss;
    Page page = Page::kHome;
    std::string asr_text;
    std::string response_text;
    std::vector<WifiNetwork> wifi_networks;
    std::string selected_ssid;
    std::string endpoint_draft;
    std::string config_message;
};

enum class CommandKind : uint8_t {
    kSetConnection,
    kSetConversation,
    kSetPreferredTransport,
    kSetActiveTransport,
    kSetAsrText,
    kAppendAsrText,
    kSetResponseText,
    kAppendResponseText,
    kClearTranscript,
    kOpenWifi,
    kClearWifiNetworks,
    kAddWifiNetwork,
    kOpenWifiKeyboard,
    kOpenEndpoint,
    kSetEndpointDraft,
    kSetConfigMessage,
    kBackHome,
    kSettingsPressed,
    kMicPressed,
    kTransportPressed,
};

struct UiCommand final {
    CommandKind kind = CommandKind::kSetConnection;
    uint32_t value = 0;
    std::string text;
};

enum class EventKind : uint8_t {
    kStartConversation,
    kStopConversation,
    kSelectTransport,
    kRequestWifiScan,
    kSaveWifi,
    kSaveEndpoint,
    kExitProvisioning,
};

struct UiEvent final {
    EventKind kind = EventKind::kStartConversation;
    Transport transport = Transport::kWss;
    std::string text;
    std::string secret;
};

inline constexpr size_t kMaxTranscriptBytes = 768;

std::optional<UiEvent> Reduce(UiState* state, const UiCommand& command);
std::string TruncateUtf8(std::string text, size_t max_bytes);
const char* ConnectionLabel(ConnectionState state);
const char* ConversationLabel(ConversationState state);
const char* TransportLabel(Transport transport);

}  // namespace rva::ui
