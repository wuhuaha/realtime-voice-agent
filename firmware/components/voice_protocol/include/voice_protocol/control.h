#pragma once

#include <array>
#include <cstdint>
#include <optional>
#include <string>
#include <variant>
#include <vector>

namespace rva::protocol {

inline constexpr size_t kMaxControlBytes = 32768;
inline constexpr size_t kMaxIdBytes = 128;

enum class ControlError {
    kOk = 0,
    kOversize,
    kMalformedJson,
    kUnknownMessage,
    kUnknownOrDuplicateField,
    kMissingOrInvalidField,
    kUnsupportedProfile,
};

enum class ServerMessageType {
    kSessionOpened,
    kTranscriptDelta,
    kTranscriptFinal,
    kResponseBegin,
    kResponseText,
    kResponseEnd,
    kPlaybackStop,
    kSessionError,
    kSessionClose,
};

enum class ResponseOutcome : uint8_t {
    kCompleted,
    kCancelled,
    kFailed,
};

enum class PlaybackEndedOutcome : uint8_t {
    kCompleted,
    kStopped,
    kFailed,
};

struct SessionIdentity final {
    std::string session_id;
    std::string session_epoch;
};

struct UdpGrant final {
    std::string host;
    uint16_t port = 0;
    uint64_t expires_at_ms = 0;
    std::array<uint8_t, 16> uplink_key{};
    std::array<uint8_t, 8> uplink_salt{};
    std::array<uint8_t, 16> downlink_key{};
    std::array<uint8_t, 8> downlink_salt{};
    uint32_t probe_timeout_ms = 0;
};

struct SessionOpened final {
    std::string request_id;
    SessionIdentity session;
    std::array<uint8_t, 8> media_id{};
    uint32_t media_epoch = 0;
    std::string selected_media_profile;
    std::optional<UdpGrant> udp_grant;
    uint32_t heartbeat_interval_ms = 0;
    uint32_t idle_timeout_ms = 0;
};

struct Transcript final {
    bool final = false;
    SessionIdentity session;
    std::string utterance_id;
    uint32_t sequence = 0;
    std::string text;
};

struct ResponseEvent final {
    ServerMessageType type = ServerMessageType::kResponseBegin;
    SessionIdentity session;
    std::string response_id;
    uint32_t generation = 0;
    uint32_t sequence = 0;
    std::string text;
    ResponseOutcome outcome = ResponseOutcome::kCompleted;
    std::optional<uint32_t> final_media_sequence;
    std::string error_code;
};

struct ResponseTarget final {
    std::string response_id;
    uint32_t generation = 0;
};

struct PlaybackStop final {
    SessionIdentity session;
    ResponseTarget target;
    uint32_t fence_generation = 0;
    std::string cause;
};

struct SessionError final {
    SessionIdentity session;
    std::string code;
    bool retryable = false;
    std::string message;
};

struct SessionClose final {
    SessionIdentity session;
    std::string reason;
    std::string initiated_by;
    std::string detail;
};

using ServerMessage =
    std::variant<SessionOpened, Transcript, ResponseEvent, PlaybackStop, SessionError, SessionClose>;

struct DeviceCapabilities final {
    bool aec = false;
    bool vad = false;
    bool wake_word = false;
    bool display = false;
    bool touch = false;
};

struct SessionOpen final {
    std::string request_id;
    std::string device_id;
    std::vector<std::string> supported_media_profiles;
    std::string preferred_media_profile;
    DeviceCapabilities capabilities;
};

struct ResponseCancelRequest final {
    SessionIdentity session;
    std::string request_id;
    ResponseTarget target;
};

struct PlaybackStarted final {
    SessionIdentity session;
    ResponseTarget target;
    uint32_t first_media_sequence = 0;
};

struct PlaybackEnded final {
    SessionIdentity session;
    ResponseTarget target;
    PlaybackEndedOutcome outcome = PlaybackEndedOutcome::kCompleted;
    uint64_t played_samples = 0;
    std::optional<uint32_t> last_media_sequence;
};

ControlError ParseServerMessage(const uint8_t* data, size_t size, ServerMessage* message);
ControlError EncodeSessionOpen(const SessionOpen& message, std::string* json);
ControlError EncodeResponseCancelRequest(const ResponseCancelRequest& message, std::string* json);
ControlError EncodePlaybackStarted(const PlaybackStarted& message, std::string* json);
ControlError EncodePlaybackEnded(const PlaybackEnded& message, std::string* json);
ControlError EncodeSessionClose(const SessionClose& message, std::string* json);

}  // namespace rva::protocol
