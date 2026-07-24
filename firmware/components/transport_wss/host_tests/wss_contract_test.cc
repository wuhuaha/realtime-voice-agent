#include "transport_wss/wss_owner.h"
#include "transport_wss/wss_session.h"
#include "voice_protocol/control.h"
#include "voice_protocol/media_header.h"

#include <array>
#include <cassert>
#include <cstring>
#include <string>
#include <variant>
#include <vector>

#include "cJSON.h"

namespace {

using rva::protocol::ControlError;
using rva::protocol::MediaDirection;
using rva::protocol::MediaError;
using rva::protocol::MediaHeader;
using rva::protocol::ResponseEvent;
using rva::protocol::ServerMessage;
using rva::protocol::ServerMessageType;
using rva::protocol::SessionIdentity;
using rva::protocol::SessionOpened;
using rva::wss::AdmissionResult;
using rva::wss::ClientEventType;

class FakeClient final : public rva::wss::EspWebsocketClientPort {
public:
    bool Start() override {
        ++start_calls;
        return true;
    }
    bool SendText(const uint8_t*, size_t size, uint32_t) override {
        ++text_calls;
        last_size = size;
        return true;
    }
    bool SendBinary(const uint8_t*, size_t size, uint32_t) override {
        ++binary_calls;
        last_size = size;
        return true;
    }
    bool Close(uint32_t) override {
        ++close_calls;
        return close_result;
    }
    bool Destroy() override {
        ++destroy_calls;
        return destroy_result;
    }

    int start_calls = 0;
    int text_calls = 0;
    int binary_calls = 0;
    int close_calls = 0;
    int destroy_calls = 0;
    bool close_result = true;
    bool destroy_result = true;
    size_t last_size = 0;
};

std::vector<uint8_t> MakeMediaFrame(
    const std::array<uint8_t, 8>& media_id,
    uint32_t media_epoch,
    uint32_t sequence,
    uint32_t generation) {
    MediaHeader header;
    header.flags = 1;
    header.media_id = media_id;
    header.media_epoch = media_epoch;
    header.sequence = sequence;
    header.timestamp = sequence * 960;
    header.generation = generation;
    header.payload_length = 3;
    std::vector<uint8_t> frame(rva::protocol::kMediaHeaderBytes + 3);
    assert(rva::protocol::SerializeMediaHeader(header, MediaDirection::kDownlink, frame.data()) == MediaError::kOk);
    frame[32] = 1;
    frame[33] = 2;
    frame[34] = 3;
    return frame;
}

void TestStrictControlParserAndEncoders() {
    const std::string opened_json =
        R"({"type":"session.opened","request_id":"open-001","session_id":"session-001",)"
        R"("session_epoch":"epoch-007","media_id":"0123456789abcdef","media_epoch":7,)"
        R"("selected_media_profile":"wss-opus-v3","audio":{"codec":"opus","sample_rate_hz":16000,)"
        R"("channels":1,"frame_duration_ms":60},"heartbeat_interval_ms":15000,"idle_timeout_ms":45000,)"
        R"("max_control_message_bytes":32768})";
    ServerMessage message;
    assert(rva::protocol::ParseServerMessage(
               reinterpret_cast<const uint8_t*>(opened_json.data()), opened_json.size(), &message) ==
           ControlError::kOk);
    const auto& opened = std::get<SessionOpened>(message);
    assert(opened.session.session_epoch == "epoch-007");
    assert(opened.media_epoch == 7);
    assert(opened.selected_media_profile == "wss-opus-v3");
    assert(!opened.udp_grant.has_value());

    const std::string opened_udp_json =
        R"({"type":"session.opened","request_id":"open-002","session_id":"session-002",)"
        R"("session_epoch":"epoch-008","media_id":"fedcba9876543210","media_epoch":8,)"
        R"("selected_media_profile":"udp-opus-gcm-v2","audio":{"codec":"opus","sample_rate_hz":16000,)"
        R"("channels":1,"frame_duration_ms":60},"heartbeat_interval_ms":15000,"idle_timeout_ms":45000,)"
        R"("max_control_message_bytes":32768,"udp_grant":{"host":"voice.example.test","port":8443,)"
        R"("expires_at_ms":1780000000000,"refresh_after_ms":595000,"uplink_key_b64":"AAAAAAAAAAAAAAAAAAAAAA==",)"
        R"("uplink_salt_b64":"AAAAAAAAAAA=","downlink_key_b64":"/////////////////////w==",)"
        R"("downlink_salt_b64":"//////////8=","probe_timeout_ms":1500}})";
    assert(rva::protocol::ParseServerMessage(
               reinterpret_cast<const uint8_t*>(opened_udp_json.data()), opened_udp_json.size(), &message) ==
           ControlError::kOk);
    const auto& opened_udp = std::get<SessionOpened>(message);
    assert(opened_udp.selected_media_profile == "udp-opus-gcm-v2");
    assert(opened_udp.udp_grant.has_value());
    assert(opened_udp.udp_grant->host == "voice.example.test");
    assert(opened_udp.udp_grant->port == 8443);
    assert(opened_udp.udp_grant->expires_at_ms == 1780000000000ULL);
    assert(opened_udp.udp_grant->refresh_after_ms == 595000);
    assert(opened_udp.udp_grant->probe_timeout_ms == 1500);
    assert(opened_udp.udp_grant->uplink_key[0] == 0 && opened_udp.udp_grant->uplink_key[15] == 0);
    assert(opened_udp.udp_grant->downlink_key[0] == 0xff && opened_udp.udp_grant->downlink_key[15] == 0xff);

    const std::vector<std::string> rejected_opened_messages = {
        R"({"type":"session.opened","request_id":"open-001","session_id":"session-001","session_epoch":"epoch-007","media_id":"0123456789abcdef","media_epoch":7,"selected_media_profile":"udp-opus-gcm-v2","audio":{"codec":"opus","sample_rate_hz":16000,"channels":1,"frame_duration_ms":60},"heartbeat_interval_ms":15000,"idle_timeout_ms":45000,"max_control_message_bytes":32768})",
        R"({"type":"session.opened","request_id":"open-001","session_id":"session-001","session_epoch":"epoch-007","media_id":"0123456789abcdef","media_epoch":7,"selected_media_profile":"wss-opus-v3","audio":{"codec":"opus","sample_rate_hz":16000,"channels":1,"frame_duration_ms":60},"heartbeat_interval_ms":15000,"idle_timeout_ms":45000,"max_control_message_bytes":32768,"udp_grant":{}})",
        R"({"type":"session.opened","request_id":"open-002","session_id":"session-002","session_epoch":"epoch-008","media_id":"fedcba9876543210","media_epoch":8,"selected_media_profile":"udp-opus-gcm-v2","audio":{"codec":"opus","sample_rate_hz":16000,"channels":1,"frame_duration_ms":60},"heartbeat_interval_ms":15000,"idle_timeout_ms":45000,"max_control_message_bytes":32768,"udp_grant":{"host":"voice.example.test","port":8443,"expires_at_ms":1780000000000,"uplink_key_b64":"AAAAAAAAAAAAAAAAAAAAAB==","uplink_salt_b64":"AAAAAAAAAAA=","downlink_key_b64":"/////////////////////w==","downlink_salt_b64":"//////////8=","probe_timeout_ms":1500}})",
    };
    for (const std::string& rejected : rejected_opened_messages) {
        assert(rva::protocol::ParseServerMessage(
                   reinterpret_cast<const uint8_t*>(rejected.data()), rejected.size(), &message) !=
               ControlError::kOk);
    }

    const std::vector<std::string> accepted_server_messages = {
        R"({"type":"transcript.delta","session_id":"session-001","session_epoch":"epoch-007",)"
        R"("utterance_id":"utt-1","sequence":0,"text":"hello"})",
        R"({"type":"transcript.final","session_id":"session-001","session_epoch":"epoch-007",)"
        R"("utterance_id":"utt-1","sequence":1,"text":"hello world"})",
        R"({"type":"response.begin","session_id":"session-001","session_epoch":"epoch-007",)"
        R"("response_id":"resp-1","generation":3})",
        R"({"type":"response.text","session_id":"session-001","session_epoch":"epoch-007",)"
        R"("response_id":"resp-1","generation":3,"sequence":0,"text":"hi"})",
        R"({"type":"response.end","session_id":"session-001","session_epoch":"epoch-007",)"
        R"("response_id":"resp-1","generation":3,"outcome":"completed","final_media_sequence":1})",
        R"({"type":"response.end","session_id":"session-001","session_epoch":"epoch-007",)"
        R"("response_id":"resp-1","generation":3,"outcome":"cancelled"})",
        R"({"type":"response.end","session_id":"session-001","session_epoch":"epoch-007",)"
        R"("response_id":"resp-1","generation":3,"outcome":"failed","error_code":"provider_timeout"})",
        R"({"type":"playback.stop","session_id":"session-001","session_epoch":"epoch-007",)"
        R"("target":{"response_id":"resp-1","generation":3},"fence_generation":4,)"
        R"("cause":"recognized_interrupt"})",
        R"({"type":"session.error","session_id":"session-001","session_epoch":"epoch-007",)"
        R"("code":"provider_timeout","retryable":true,"message":"timeout"})",
        R"({"type":"session.close","session_id":"session-001","session_epoch":"epoch-007",)"
        R"("reason":"normal","initiated_by":"server"})",
    };
    for (const std::string& control : accepted_server_messages) {
        assert(rva::protocol::ParseServerMessage(
                   reinterpret_cast<const uint8_t*>(control.data()), control.size(), &message) ==
               ControlError::kOk);
    }

    const std::string unknown =
        R"({"type":"session.close","session_id":"session-001","session_epoch":"epoch-007",)"
        R"("reason":"normal","initiated_by":"server","mode":"legacy"})";
    assert(rva::protocol::ParseServerMessage(
               reinterpret_cast<const uint8_t*>(unknown.data()), unknown.size(), &message) ==
           ControlError::kUnknownOrDuplicateField);
    const std::string duplicate =
        R"({"type":"response.begin","session_id":"session-001","session_epoch":"epoch-007",)"
        R"("response_id":"resp-1","generation":1,"generation":2})";
    assert(rva::protocol::ParseServerMessage(
               reinterpret_cast<const uint8_t*>(duplicate.data()), duplicate.size(), &message) ==
           ControlError::kUnknownOrDuplicateField);
    const std::string escaped_null =
        R"({"type":"response.begin","session_id":"session\u0000hidden","session_epoch":"epoch-007",)"
        R"("response_id":"resp-1","generation":1})";
    assert(rva::protocol::ParseServerMessage(
               reinterpret_cast<const uint8_t*>(escaped_null.data()), escaped_null.size(), &message) ==
           ControlError::kMalformedJson);
    std::vector<uint8_t> oversized(rva::protocol::kMaxControlBytes + 1, 'x');
    assert(rva::protocol::ParseServerMessage(oversized.data(), oversized.size(), &message) ==
           ControlError::kOversize);

    const std::vector<std::string> rejected_v2_messages = {
        R"({"type":"response.end","session_id":"session-001","session_epoch":"epoch-007","response_id":"resp-1","generation":3,"outcome":"completed"})",
        R"({"type":"response.end","session_id":"session-001","session_epoch":"epoch-007","response_id":"resp-1","generation":3,"outcome":"cancelled","final_media_sequence":1})",
        R"({"type":"response.end","session_id":"session-001","session_epoch":"epoch-007","response_id":"resp-1","generation":3,"outcome":"failed","error_code":"Bad-Code"})",
        R"({"type":"playback.stop","session_id":"session-001","session_epoch":"epoch-007","target":{"response_id":"resp-1","generation":3},"fence_generation":3,"cause":"recognized_interrupt"})",
        R"({"type":"response.cancelled","session_id":"session-001","session_epoch":"epoch-007","target":{"response_id":"resp-1","generation":3},"reason":"cancelled"})",
    };
    for (const std::string& rejected : rejected_v2_messages) {
        assert(rva::protocol::ParseServerMessage(
                   reinterpret_cast<const uint8_t*>(rejected.data()), rejected.size(), &message) !=
               ControlError::kOk);
    }

    rva::protocol::SessionOpen open;
    open.request_id = "open-001";
    open.device_id = "esp32s3-test";
    open.supported_media_profiles = {"wss-opus-v3"};
    open.preferred_media_profile = "wss-opus-v3";
    open.capabilities = {true, true, true, true, true};
    std::string encoded;
    assert(rva::protocol::EncodeSessionOpen(open, &encoded) == ControlError::kOk);
    cJSON* root = cJSON_ParseWithLength(encoded.data(), encoded.size());
    assert(root != nullptr);
    assert(std::strcmp(cJSON_GetStringValue(cJSON_GetObjectItem(root, "type")), "session.open") == 0);
    assert(cJSON_GetNumberValue(cJSON_GetObjectItem(root, "protocol_version")) == 2);
    cJSON_Delete(root);

    assert(rva::protocol::EncodeResponseCancelRequest(
               {{"session-001", "epoch-007"}, "cancel-001", {"resp-1", 3}},
               &encoded) == ControlError::kOk);
    root = cJSON_ParseWithLength(encoded.data(), encoded.size());
    assert(root != nullptr);
    const cJSON* target = cJSON_GetObjectItem(root, "target");
    assert(cJSON_GetNumberValue(cJSON_GetObjectItem(target, "generation")) == 3);
    assert(std::strcmp(cJSON_GetStringValue(cJSON_GetObjectItem(root, "cause")), "user_request") == 0);
    cJSON_Delete(root);

    assert(rva::protocol::EncodePlaybackStarted(
               {{"session-001", "epoch-007"}, {"resp-1", 3}, 0}, &encoded) == ControlError::kOk);
    assert(rva::protocol::EncodePlaybackEnded(
               {{"session-001", "epoch-007"}, {"resp-1", 3},
                rva::protocol::PlaybackEndedOutcome::kCompleted, 2880, 1},
               &encoded) == ControlError::kOk);
    assert(rva::protocol::EncodePlaybackEnded(
               {{"session-001", "epoch-007"}, {"resp-1", 3},
                rva::protocol::PlaybackEndedOutcome::kStopped, 0, 1},
               &encoded) == ControlError::kMissingOrInvalidField);

    rva::protocol::SessionClose close;
    close.session = {"session-001", "epoch-007"};
    close.reason = "normal";
    close.initiated_by = "device";
    assert(rva::protocol::EncodeSessionClose(close, &encoded) == ControlError::kOk);
}

void TestMediaHeaderStrictRoundTrip() {
    MediaHeader header;
    header.flags = 1;
    header.media_id = {0x01, 0x23, 0x45, 0x67, 0x89, 0xab, 0xcd, 0xef};
    header.media_epoch = 7;
    header.sequence = 9;
    header.timestamp = 8640;
    header.generation = 3;
    header.payload_length = 4;
    std::array<uint8_t, rva::protocol::kMediaHeaderBytes + 4> frame{};
    assert(rva::protocol::SerializeMediaHeader(header, MediaDirection::kDownlink, frame.data()) == MediaError::kOk);
    MediaHeader parsed;
    assert(rva::protocol::ParseMediaHeader(frame.data(), frame.size(), MediaDirection::kDownlink, &parsed) ==
           MediaError::kOk);
    assert(parsed.sequence == 9 && parsed.timestamp == 8640 && parsed.media_id == header.media_id);

    frame[0] = 0;
    assert(rva::protocol::ParseMediaHeader(frame.data(), frame.size(), MediaDirection::kDownlink, &parsed) ==
           MediaError::kInvalidMagic);
    frame[0] = 0x56;
    frame[28] = 0;
    frame[29] = 0;
    frame[30] = 0;
    frame[31] = 3;
    assert(rva::protocol::ParseMediaHeader(frame.data(), frame.size(), MediaDirection::kDownlink, &parsed) ==
           MediaError::kInvalidPayloadLength);
}

void TestSessionIdentityGenerationAndSequenceFences() {
    rva::wss::WssSession session("open-001");
    SessionOpened opened;
    opened.request_id = "open-001";
    opened.session = {"session-001", "epoch-007"};
    opened.media_id = {0x01, 0x23, 0x45, 0x67, 0x89, 0xab, 0xcd, 0xef};
    opened.media_epoch = 7;
    assert(session.Accept(opened) == AdmissionResult::kAccepted);

    rva::protocol::Transcript transcript;
    transcript.session = opened.session;
    transcript.utterance_id = "utt-1";
    transcript.text = "hello";
    assert(session.Accept(transcript) == AdmissionResult::kAccepted);
    assert(session.Accept(transcript) == AdmissionResult::kInvalidSequence);
    transcript.sequence = 1;
    transcript.final = true;
    assert(session.Accept(transcript) == AdmissionResult::kAccepted);

    ResponseEvent begin;
    begin.type = ServerMessageType::kResponseBegin;
    begin.session = opened.session;
    begin.response_id = "resp-1";
    begin.generation = 3;
    assert(session.Accept(begin) == AdmissionResult::kAccepted);

    ResponseEvent text = begin;
    text.type = ServerMessageType::kResponseText;
    text.text = "hi";
    assert(session.Accept(text) == AdmissionResult::kAccepted);
    assert(session.Accept(text) == AdmissionResult::kInvalidSequence);

    auto media = MakeMediaFrame(opened.media_id, opened.media_epoch, 0, 3);
    MediaHeader parsed;
    assert(session.AcceptMedia(media.data(), media.size(), &parsed) == AdmissionResult::kAccepted);
    assert(session.AcceptMedia(media.data(), media.size(), &parsed) == AdmissionResult::kInvalidSequence);
    media = MakeMediaFrame(opened.media_id, opened.media_epoch - 1, 1, 3);
    assert(session.AcceptMedia(media.data(), media.size(), &parsed) == AdmissionResult::kStaleMediaIdentity);

    std::string cancel;
    assert(session.EncodeCancelRequest("cancel-001", &cancel) == ControlError::kOk);
    rva::protocol::PlaybackStop stop;
    stop.session = opened.session;
    stop.target = {"resp-1", 3};
    stop.fence_generation = 4;
    stop.cause = "recognized_interrupt";
    assert(session.Accept(stop) == AdmissionResult::kAccepted);
    assert(session.Accept(stop) == AdmissionResult::kAccepted);
    stop.fence_generation = 5;
    assert(session.Accept(stop) == AdmissionResult::kPlaybackStopConflict);
    stop.fence_generation = 4;
    stop.cause = "session_close";
    assert(session.Accept(stop) == AdmissionResult::kPlaybackStopConflict);
    stop.cause = "recognized_interrupt";
    assert(session.playback_fence() == 4);
    ResponseEvent cancelled = begin;
    cancelled.type = ServerMessageType::kResponseEnd;
    cancelled.outcome = rva::protocol::ResponseOutcome::kCancelled;
    assert(session.Accept(cancelled) == AdmissionResult::kAccepted);
    assert(session.Accept(cancelled) == AdmissionResult::kResponseAlreadyTerminal);
    media = MakeMediaFrame(opened.media_id, opened.media_epoch, 1, 3);
    assert(session.AcceptMedia(media.data(), media.size(), &parsed) == AdmissionResult::kStaleGeneration);

    begin.generation = 4;
    assert(session.Accept(begin) == AdmissionResult::kStaleGeneration);
    rva::protocol::SessionClose close;
    close.session = opened.session;
    close.reason = "normal";
    close.initiated_by = "server";
    assert(session.Accept(close) == AdmissionResult::kAccepted);
    assert(session.Accept(begin) == AdmissionResult::kSessionClosed);
}

void TestCallbackOnlyQueuesAndSupervisorOwnsTeardown() {
    FakeClient client;
    {
        rva::wss::WssOwner owner(client, 2, 16);
        assert(owner.Start());
        const std::array<uint8_t, 3> first = {'a', 'b', 'c'};
        const std::array<uint8_t, 2> second = {'d', 'e'};
        assert(owner.OnClientCallback({ClientEventType::kTextFragment, first.data(), first.size(), 0, 5}));
        assert(owner.OnClientCallback({ClientEventType::kTextFragment, second.data(), second.size(), 3, 5}));
        assert(!owner.OnClientCallback({ClientEventType::kConnected}));
        assert(owner.dropped_events() == 1);
        assert(client.close_calls == 0 && client.destroy_calls == 0);

        rva::wss::OwnedClientEvent event;
        rva::wss::FrameAssembler assembler;
        std::vector<uint8_t> frame;
        assert(owner.Poll(&event));
        assert(assembler.Consume(event, &frame) == rva::wss::AssembleResult::kIncomplete);
        assert(owner.Poll(&event));
        assert(assembler.Consume(event, &frame) == rva::wss::AssembleResult::kComplete);
        assert(std::string(frame.begin(), frame.end()) == "abcde");

        owner.RequestClose();
        assert(client.close_calls == 0 && client.destroy_calls == 0);
        assert(!owner.SendText("blocked", 10));
        assert(owner.SupervisorClose(100));
        assert(client.close_calls == 1 && client.destroy_calls == 1);
    }
    assert(client.close_calls == 1 && client.destroy_calls == 1);
}

void TestTeardownFailureIsObservableAndRetryable() {
    FakeClient client;
    client.close_result = false;
    client.destroy_result = false;
    {
        rva::wss::WssOwner owner(client, 1, 8);
        assert(owner.Start());
        owner.RequestClose();
        assert(!owner.SupervisorClose(100));
        assert(client.close_calls == 1 && client.destroy_calls == 1);

        client.close_result = true;
        client.destroy_result = true;
        assert(owner.SupervisorClose(100));
        assert(client.close_calls == 2 && client.destroy_calls == 2);
    }
    assert(client.close_calls == 2 && client.destroy_calls == 2);
}

}  // namespace

int main() {
    TestStrictControlParserAndEncoders();
    TestMediaHeaderStrictRoundTrip();
    TestSessionIdentityGenerationAndSequenceFences();
    TestCallbackOnlyQueuesAndSupervisorOwnsTeardown();
    TestTeardownFailureIsObservableAndRetryable();
    return 0;
}
