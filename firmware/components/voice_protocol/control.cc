#include "voice_protocol/control.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <initializer_list>
#include <limits>
#include <memory>
#include <string_view>

#include "cJSON.h"

namespace rva::protocol {
namespace {

struct JsonDeleter {
    void operator()(cJSON* value) const { cJSON_Delete(value); }
};
using Json = std::unique_ptr<cJSON, JsonDeleter>;

bool IsIdentifier(std::string_view value) {
    return !value.empty() && value.size() <= kMaxIdBytes &&
           std::all_of(value.begin(), value.end(), [](unsigned char character) {
               return (character >= 'a' && character <= 'z') ||
                      (character >= 'A' && character <= 'Z') ||
                      (character >= '0' && character <= '9') || character == '.' ||
                      character == '_' || character == ':' || character == '-';
           });
}

bool HasExactFields(
    const cJSON* object,
    std::initializer_list<const char*> required,
    std::initializer_list<const char*> optional = {}) {
    if (!cJSON_IsObject(object)) {
        return false;
    }
    for (const char* field : required) {
        if (cJSON_GetObjectItemCaseSensitive(object, field) == nullptr) {
            return false;
        }
    }
    for (const cJSON* item = object->child; item != nullptr; item = item->next) {
        if (item->string == nullptr) {
            return false;
        }
        const auto matches = [item](const char* field) { return std::strcmp(item->string, field) == 0; };
        if (std::none_of(required.begin(), required.end(), matches) &&
            std::none_of(optional.begin(), optional.end(), matches)) {
            return false;
        }
        for (const cJSON* previous = object->child; previous != item; previous = previous->next) {
            if (previous->string != nullptr && std::strcmp(previous->string, item->string) == 0) {
                return false;
            }
        }
    }
    return true;
}

bool GetString(
    const cJSON* object,
    const char* field,
    size_t maximum_bytes,
    bool allow_empty,
    std::string* output) {
    const cJSON* item = cJSON_GetObjectItemCaseSensitive(object, field);
    if (!cJSON_IsString(item) || item->valuestring == nullptr) {
        return false;
    }
    const size_t size = std::strlen(item->valuestring);
    if ((!allow_empty && size == 0) || size > maximum_bytes) {
        return false;
    }
    output->assign(item->valuestring, size);
    return true;
}

bool GetId(const cJSON* object, const char* field, std::string* output) {
    return GetString(object, field, kMaxIdBytes, false, output) && IsIdentifier(*output);
}

bool GetU32(const cJSON* object, const char* field, bool allow_zero, uint32_t* output) {
    const cJSON* item = cJSON_GetObjectItemCaseSensitive(object, field);
    if (!cJSON_IsNumber(item) || !std::isfinite(item->valuedouble) ||
        std::floor(item->valuedouble) != item->valuedouble || item->valuedouble < (allow_zero ? 0 : 1) ||
        item->valuedouble > std::numeric_limits<uint32_t>::max()) {
        return false;
    }
    *output = static_cast<uint32_t>(item->valuedouble);
    return true;
}

bool GetU64(const cJSON* object, const char* field, uint64_t* output) {
    const cJSON* item = cJSON_GetObjectItemCaseSensitive(object, field);
    constexpr double kMaxExactJsonInteger = 9007199254740991.0;
    if (!cJSON_IsNumber(item) || !std::isfinite(item->valuedouble) ||
        std::floor(item->valuedouble) != item->valuedouble || item->valuedouble < 1 ||
        item->valuedouble > kMaxExactJsonInteger) {
        return false;
    }
    *output = static_cast<uint64_t>(item->valuedouble);
    return true;
}

bool GetSession(const cJSON* object, SessionIdentity* session) {
    return GetId(object, "session_id", &session->session_id) &&
           GetId(object, "session_epoch", &session->session_epoch);
}

bool IsOneOf(std::string_view value, std::initializer_list<std::string_view> allowed) {
    return std::find(allowed.begin(), allowed.end(), value) != allowed.end();
}

bool ParseMediaId(const std::string& hex, std::array<uint8_t, 8>* output) {
    if (hex.size() != 16) {
        return false;
    }
    const auto nibble = [](unsigned char value) -> int {
        if (value >= '0' && value <= '9') return value - '0';
        if (value >= 'a' && value <= 'f') return value - 'a' + 10;
        return -1;
    };
    for (size_t index = 0; index < output->size(); ++index) {
        const int high = nibble(hex[index * 2]);
        const int low = nibble(hex[index * 2 + 1]);
        if (high < 0 || low < 0) return false;
        (*output)[index] = static_cast<uint8_t>((high << 4) | low);
    }
    return true;
}

int Base64Value(unsigned char value) {
    if (value >= 'A' && value <= 'Z') return value - 'A';
    if (value >= 'a' && value <= 'z') return value - 'a' + 26;
    if (value >= '0' && value <= '9') return value - '0' + 52;
    if (value == '+') return 62;
    if (value == '/') return 63;
    return -1;
}

template <size_t Size>
bool DecodeFixedBase64(const std::string& encoded, std::array<uint8_t, Size>* output) {
    constexpr size_t kEncodedSize = ((Size + 2) / 3) * 4;
    if (encoded.size() != kEncodedSize) return false;

    size_t written = 0;
    for (size_t index = 0; index < encoded.size(); index += 4) {
        const bool last = index + 4 == encoded.size();
        const int first = Base64Value(encoded[index]);
        const int second = Base64Value(encoded[index + 1]);
        const bool third_padding = encoded[index + 2] == '=';
        const bool fourth_padding = encoded[index + 3] == '=';
        const int third = third_padding ? 0 : Base64Value(encoded[index + 2]);
        const int fourth = fourth_padding ? 0 : Base64Value(encoded[index + 3]);
        if (first < 0 || second < 0 || third < 0 || fourth < 0 ||
            (!last && (third_padding || fourth_padding)) || (third_padding && !fourth_padding)) {
            return false;
        }

        const size_t remaining = Size - written;
        if (third_padding) {
            if (!last || remaining != 1 || (second & 0x0f) != 0) return false;
        } else if (fourth_padding) {
            if (!last || remaining != 2 || (third & 0x03) != 0) return false;
        } else if (remaining < 3) {
            return false;
        }

        (*output)[written++] = static_cast<uint8_t>((first << 2) | (second >> 4));
        if (!third_padding) {
            (*output)[written++] = static_cast<uint8_t>((second << 4) | (third >> 2));
        }
        if (!fourth_padding) {
            (*output)[written++] = static_cast<uint8_t>((third << 6) | fourth);
        }
    }
    return written == Size;
}

bool ParseUdpGrant(const cJSON* object, UdpGrant* grant) {
    if (!HasExactFields(
            object,
            {"host", "port", "expires_at_ms", "uplink_key_b64", "uplink_salt_b64",
             "downlink_key_b64", "downlink_salt_b64", "probe_timeout_ms"})) {
        return false;
    }
    uint32_t port = 0;
    std::string uplink_key;
    std::string uplink_salt;
    std::string downlink_key;
    std::string downlink_salt;
    if (!GetString(object, "host", 253, false, &grant->host) ||
        !GetU32(object, "port", false, &port) || port > 65535 ||
        !GetU64(object, "expires_at_ms", &grant->expires_at_ms) ||
        !GetString(object, "uplink_key_b64", 24, false, &uplink_key) ||
        !DecodeFixedBase64(uplink_key, &grant->uplink_key) ||
        !GetString(object, "uplink_salt_b64", 12, false, &uplink_salt) ||
        !DecodeFixedBase64(uplink_salt, &grant->uplink_salt) ||
        !GetString(object, "downlink_key_b64", 24, false, &downlink_key) ||
        !DecodeFixedBase64(downlink_key, &grant->downlink_key) ||
        !GetString(object, "downlink_salt_b64", 12, false, &downlink_salt) ||
        !DecodeFixedBase64(downlink_salt, &grant->downlink_salt) ||
        !GetU32(object, "probe_timeout_ms", false, &grant->probe_timeout_ms) ||
        grant->probe_timeout_ms < 100 || grant->probe_timeout_ms > 10000) {
        return false;
    }
    grant->port = static_cast<uint16_t>(port);
    return true;
}

bool IsAudioProfile(const cJSON* audio) {
    std::string codec;
    uint32_t sample_rate = 0;
    uint32_t channels = 0;
    uint32_t duration = 0;
    return HasExactFields(audio, {"codec", "sample_rate_hz", "channels", "frame_duration_ms"}) &&
           GetString(audio, "codec", 8, false, &codec) && codec == "opus" &&
           GetU32(audio, "sample_rate_hz", false, &sample_rate) && sample_rate == 16000 &&
           GetU32(audio, "channels", false, &channels) && channels == 1 &&
           GetU32(audio, "frame_duration_ms", false, &duration) && duration == 60;
}

ControlError ParseOpened(const cJSON* root, ServerMessage* message) {
    if (!HasExactFields(
            root,
            {"type", "request_id", "session_id", "session_epoch", "media_id", "media_epoch",
             "selected_media_profile", "audio", "heartbeat_interval_ms", "idle_timeout_ms",
             "max_control_message_bytes"},
            {"udp_grant"})) {
        return ControlError::kUnknownOrDuplicateField;
    }
    SessionOpened opened;
    std::string media_id;
    std::string profile;
    uint32_t max_control = 0;
    if (!GetId(root, "request_id", &opened.request_id) || !GetSession(root, &opened.session) ||
        !GetString(root, "media_id", 16, false, &media_id) || !ParseMediaId(media_id, &opened.media_id) ||
        !GetU32(root, "media_epoch", false, &opened.media_epoch) ||
        !GetString(root, "selected_media_profile", 32, false, &profile) ||
        !IsOneOf(profile, {"wss-opus-v2", "udp-opus-gcm-v1"}) ||
        !IsAudioProfile(cJSON_GetObjectItemCaseSensitive(root, "audio")) ||
        !GetU32(root, "heartbeat_interval_ms", false, &opened.heartbeat_interval_ms) ||
        opened.heartbeat_interval_ms < 5000 || opened.heartbeat_interval_ms > 60000 ||
        !GetU32(root, "idle_timeout_ms", false, &opened.idle_timeout_ms) || opened.idle_timeout_ms < 15000 ||
        opened.idle_timeout_ms > 180000 || !GetU32(root, "max_control_message_bytes", false, &max_control) ||
        max_control != kMaxControlBytes) {
        return profile.empty() || IsOneOf(profile, {"wss-opus-v2", "udp-opus-gcm-v1"})
                   ? ControlError::kMissingOrInvalidField
                   : ControlError::kUnsupportedProfile;
    }
    opened.selected_media_profile = profile;
    const cJSON* udp_grant = cJSON_GetObjectItemCaseSensitive(root, "udp_grant");
    if (profile == "udp-opus-gcm-v1") {
        UdpGrant parsed_grant;
        if (!ParseUdpGrant(udp_grant, &parsed_grant)) return ControlError::kMissingOrInvalidField;
        opened.udp_grant = std::move(parsed_grant);
    } else if (udp_grant != nullptr) {
        return ControlError::kMissingOrInvalidField;
    }
    *message = std::move(opened);
    return ControlError::kOk;
}

ControlError ParseTranscript(const cJSON* root, bool final, ServerMessage* message) {
    if (!HasExactFields(root, {"type", "session_id", "session_epoch", "utterance_id", "sequence", "text"})) {
        return ControlError::kUnknownOrDuplicateField;
    }
    Transcript transcript;
    transcript.final = final;
    const size_t text_limit = final ? 16384 : 4096;
    if (!GetSession(root, &transcript.session) || !GetId(root, "utterance_id", &transcript.utterance_id) ||
        !GetU32(root, "sequence", true, &transcript.sequence) ||
        !GetString(root, "text", text_limit, final, &transcript.text)) {
        return ControlError::kMissingOrInvalidField;
    }
    *message = std::move(transcript);
    return ControlError::kOk;
}

ControlError ParseResponse(const cJSON* root, ServerMessageType type, ServerMessage* message) {
    const bool has_sequence = type == ServerMessageType::kResponseText;
    const bool has_text = type == ServerMessageType::kResponseText;
    const bool has_reason = type == ServerMessageType::kResponseEnd;
    if ((type == ServerMessageType::kResponseBegin &&
         !HasExactFields(root, {"type", "session_id", "session_epoch", "response_id", "generation"})) ||
        (type == ServerMessageType::kResponseText &&
         !HasExactFields(
             root,
             {"type", "session_id", "session_epoch", "response_id", "generation", "sequence", "text"})) ||
        (type == ServerMessageType::kResponseEnd &&
         !HasExactFields(root, {"type", "session_id", "session_epoch", "response_id", "generation", "reason"}))) {
        return ControlError::kUnknownOrDuplicateField;
    }
    ResponseEvent event;
    event.type = type;
    if (!GetSession(root, &event.session) || !GetId(root, "response_id", &event.response_id) ||
        !GetU32(root, "generation", false, &event.generation) ||
        (has_sequence && !GetU32(root, "sequence", true, &event.sequence)) ||
        (has_text && !GetString(root, "text", 4096, false, &event.text)) ||
        (has_reason && (!GetString(root, "reason", 16, false, &event.reason) ||
                        !IsOneOf(event.reason, {"completed", "failed"})))) {
        return ControlError::kMissingOrInvalidField;
    }
    *message = std::move(event);
    return ControlError::kOk;
}

ControlError ParseCancelled(const cJSON* root, ServerMessage* message) {
    if (!HasExactFields(root, {"type", "session_id", "session_epoch", "target", "reason"})) {
        return ControlError::kUnknownOrDuplicateField;
    }
    const cJSON* target = cJSON_GetObjectItemCaseSensitive(root, "target");
    ResponseEvent event;
    event.type = ServerMessageType::kResponseCancelled;
    if (!GetSession(root, &event.session) || !HasExactFields(target, {"response_id", "generation"}) ||
        !GetId(target, "response_id", &event.response_id) ||
        !GetU32(target, "generation", false, &event.generation) ||
        !GetString(root, "reason", 32, false, &event.reason) ||
        !IsOneOf(event.reason, {"cancelled", "already_cancelled", "already_finished"})) {
        return ControlError::kMissingOrInvalidField;
    }
    *message = std::move(event);
    return ControlError::kOk;
}

ControlError ParseError(const cJSON* root, ServerMessage* message) {
    if (!HasExactFields(root, {"type", "session_id", "session_epoch", "code", "retryable", "message"})) {
        return ControlError::kUnknownOrDuplicateField;
    }
    SessionError error;
    const cJSON* retryable = cJSON_GetObjectItemCaseSensitive(root, "retryable");
    if (!GetSession(root, &error.session) || !GetString(root, "code", 64, false, &error.code) ||
        !std::all_of(error.code.begin(), error.code.end(), [](unsigned char value) {
            return (value >= 'a' && value <= 'z') || (value >= '0' && value <= '9') || value == '_';
        }) ||
        !cJSON_IsBool(retryable) || !GetString(root, "message", 512, true, &error.message)) {
        return ControlError::kMissingOrInvalidField;
    }
    error.retryable = cJSON_IsTrue(retryable);
    *message = std::move(error);
    return ControlError::kOk;
}

ControlError ParseClose(const cJSON* root, ServerMessage* message) {
    if (!HasExactFields(root, {"type", "session_id", "session_epoch", "reason", "initiated_by"}, {"detail"})) {
        return ControlError::kUnknownOrDuplicateField;
    }
    SessionClose close;
    if (!GetSession(root, &close.session) || !GetString(root, "reason", 32, false, &close.reason) ||
        !IsOneOf(close.reason, {"normal", "idle_timeout", "network_change", "protocol_error", "server_shutdown"}) ||
        !GetString(root, "initiated_by", 8, false, &close.initiated_by) ||
        !IsOneOf(close.initiated_by, {"device", "server"})) {
        return ControlError::kMissingOrInvalidField;
    }
    const cJSON* detail = cJSON_GetObjectItemCaseSensitive(root, "detail");
    if (detail != nullptr && !GetString(root, "detail", 256, true, &close.detail)) {
        return ControlError::kMissingOrInvalidField;
    }
    *message = std::move(close);
    return ControlError::kOk;
}

bool AddString(cJSON* object, const char* field, const std::string& value) {
    return cJSON_AddStringToObject(object, field, value.c_str()) != nullptr;
}

bool AddSession(cJSON* object, const SessionIdentity& session) {
    return IsIdentifier(session.session_id) && IsIdentifier(session.session_epoch) &&
           AddString(object, "session_id", session.session_id) &&
           AddString(object, "session_epoch", session.session_epoch);
}

ControlError Print(Json root, std::string* output) {
    if (output == nullptr || root == nullptr) return ControlError::kMissingOrInvalidField;
    char* printed = cJSON_PrintUnformatted(root.get());
    if (printed == nullptr) return ControlError::kMissingOrInvalidField;
    const size_t size = std::strlen(printed);
    if (size > kMaxControlBytes) {
        cJSON_free(printed);
        return ControlError::kOversize;
    }
    output->assign(printed, size);
    cJSON_free(printed);
    return ControlError::kOk;
}

bool AddAudio(cJSON* root) {
    cJSON* audio = cJSON_AddObjectToObject(root, "audio");
    return audio != nullptr && cJSON_AddStringToObject(audio, "codec", "opus") != nullptr &&
           cJSON_AddNumberToObject(audio, "sample_rate_hz", 16000) != nullptr &&
           cJSON_AddNumberToObject(audio, "channels", 1) != nullptr &&
           cJSON_AddNumberToObject(audio, "frame_duration_ms", 60) != nullptr;
}

}  // namespace

ControlError ParseServerMessage(const uint8_t* data, size_t size, ServerMessage* message) {
    if (data == nullptr || message == nullptr || size == 0) return ControlError::kMalformedJson;
    if (size > kMaxControlBytes) return ControlError::kOversize;
    if (std::find(data, data + size, 0) != data + size) return ControlError::kMalformedJson;
    constexpr std::array<uint8_t, 6> escaped_null = {'\\', 'u', '0', '0', '0', '0'};
    if (std::search(data, data + size, escaped_null.begin(), escaped_null.end()) != data + size) {
        return ControlError::kMalformedJson;
    }
    const char* end = nullptr;
    Json root(cJSON_ParseWithLengthOpts(reinterpret_cast<const char*>(data), size, &end, false));
    if (root == nullptr || end != reinterpret_cast<const char*>(data) + size || !cJSON_IsObject(root.get())) {
        return ControlError::kMalformedJson;
    }
    std::string type;
    if (!GetString(root.get(), "type", 32, false, &type)) return ControlError::kMissingOrInvalidField;
    if (type == "session.opened") return ParseOpened(root.get(), message);
    if (type == "transcript.delta") return ParseTranscript(root.get(), false, message);
    if (type == "transcript.final") return ParseTranscript(root.get(), true, message);
    if (type == "response.begin") return ParseResponse(root.get(), ServerMessageType::kResponseBegin, message);
    if (type == "response.text") return ParseResponse(root.get(), ServerMessageType::kResponseText, message);
    if (type == "response.end") return ParseResponse(root.get(), ServerMessageType::kResponseEnd, message);
    if (type == "response.cancelled") return ParseCancelled(root.get(), message);
    if (type == "session.error") return ParseError(root.get(), message);
    if (type == "session.close") return ParseClose(root.get(), message);
    return ControlError::kUnknownMessage;
}

ControlError EncodeSessionOpen(const SessionOpen& message, std::string* json) {
    if (!IsIdentifier(message.request_id) || !IsIdentifier(message.device_id) ||
        message.supported_media_profiles.empty() || message.supported_media_profiles.size() > 2 ||
        std::find(message.supported_media_profiles.begin(), message.supported_media_profiles.end(),
                  message.preferred_media_profile) == message.supported_media_profiles.end()) {
        return ControlError::kMissingOrInvalidField;
    }
    Json root(cJSON_CreateObject());
    if (root == nullptr || !AddString(root.get(), "type", "session.open") ||
        cJSON_AddNumberToObject(root.get(), "protocol_version", 1) == nullptr ||
        !AddString(root.get(), "request_id", message.request_id) ||
        !AddString(root.get(), "device_id", message.device_id)) {
        return ControlError::kMissingOrInvalidField;
    }
    cJSON* profiles = cJSON_AddArrayToObject(root.get(), "supported_media_profiles");
    if (profiles == nullptr) return ControlError::kMissingOrInvalidField;
    for (size_t index = 0; index < message.supported_media_profiles.size(); ++index) {
        const std::string& profile = message.supported_media_profiles[index];
        if (!IsOneOf(profile, {"wss-opus-v2", "udp-opus-gcm-v1"}) ||
            std::find(message.supported_media_profiles.begin(),
                      message.supported_media_profiles.begin() + index, profile) !=
                message.supported_media_profiles.begin() + index ||
            cJSON_AddItemToArray(profiles, cJSON_CreateString(profile.c_str())) == 0) {
            return ControlError::kMissingOrInvalidField;
        }
    }
    if (!AddString(root.get(), "preferred_media_profile", message.preferred_media_profile) || !AddAudio(root.get())) {
        return ControlError::kMissingOrInvalidField;
    }
    cJSON* capabilities = cJSON_AddObjectToObject(root.get(), "capabilities");
    if (capabilities == nullptr || cJSON_AddBoolToObject(capabilities, "aec", message.capabilities.aec) == nullptr ||
        cJSON_AddBoolToObject(capabilities, "vad", message.capabilities.vad) == nullptr ||
        cJSON_AddBoolToObject(capabilities, "wake_word", message.capabilities.wake_word) == nullptr ||
        cJSON_AddBoolToObject(capabilities, "display", message.capabilities.display) == nullptr ||
        cJSON_AddBoolToObject(capabilities, "touch", message.capabilities.touch) == nullptr) {
        return ControlError::kMissingOrInvalidField;
    }
    return Print(std::move(root), json);
}

ControlError EncodeResponseCancel(
    const SessionIdentity& session,
    const CancelTarget& target,
    const std::string& reason,
    std::string* json) {
    if (!IsIdentifier(target.response_id) || target.generation == 0 ||
        !IsOneOf(reason, {"user_request", "barge_in", "new_wake", "session_close"})) {
        return ControlError::kMissingOrInvalidField;
    }
    Json root(cJSON_CreateObject());
    if (root == nullptr || !AddString(root.get(), "type", "response.cancel") || !AddSession(root.get(), session)) {
        return ControlError::kMissingOrInvalidField;
    }
    cJSON* target_json = cJSON_AddObjectToObject(root.get(), "target");
    if (target_json == nullptr || !AddString(target_json, "response_id", target.response_id) ||
        cJSON_AddNumberToObject(target_json, "generation", target.generation) == nullptr ||
        !AddString(root.get(), "reason", reason)) {
        return ControlError::kMissingOrInvalidField;
    }
    return Print(std::move(root), json);
}

ControlError EncodeSessionClose(const SessionClose& message, std::string* json) {
    if (!IsOneOf(message.reason, {"normal", "idle_timeout", "network_change", "protocol_error", "server_shutdown"}) ||
        !IsOneOf(message.initiated_by, {"device", "server"}) || message.detail.size() > 256) {
        return ControlError::kMissingOrInvalidField;
    }
    Json root(cJSON_CreateObject());
    if (root == nullptr || !AddString(root.get(), "type", "session.close") ||
        !AddSession(root.get(), message.session) || !AddString(root.get(), "reason", message.reason) ||
        !AddString(root.get(), "initiated_by", message.initiated_by) ||
        (!message.detail.empty() && !AddString(root.get(), "detail", message.detail))) {
        return ControlError::kMissingOrInvalidField;
    }
    return Print(std::move(root), json);
}

}  // namespace rva::protocol
