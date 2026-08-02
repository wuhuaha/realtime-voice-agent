#include "transport_wss/wss_owner.h"
#include "transport_wss/wss_session.h"
#include "voice_protocol/control.h"
#include "voice_protocol/media_header.h"

#include <array>
#include <atomic>
#include <cassert>
#include <chrono>
#include <condition_variable>
#include <cstring>
#include <mutex>
#include <string>
#include <thread>
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

class BlockingCallbackClient final : public rva::wss::EspWebsocketClientPort {
public:
    void BindEventSink(rva::wss::WssOwner* owner) { owner_ = owner; }

    bool Start() override {
        std::lock_guard<std::mutex> lock(callback_mutex_);
        ++start_calls;
        callbacks_enabled_ = true;
        return true;
    }
    bool SendText(const uint8_t*, size_t, uint32_t) override { return true; }
    bool SendBinary(const uint8_t*, size_t, uint32_t) override { return true; }
    bool Close(uint32_t) override {
        ++close_calls;
        return true;
    }
    bool Destroy() override {
        std::unique_lock<std::mutex> lock(callback_mutex_);
        ++destroy_calls;
        callbacks_enabled_ = false;
        destroy_entered.store(true, std::memory_order_release);
        callback_finished_.wait(lock, [this]() { return callbacks_in_flight_ == 0; });
        destroyed_ = true;
        return true;
    }

    bool Emit(const rva::wss::ClientEventView& event) {
        {
            std::lock_guard<std::mutex> lock(callback_mutex_);
            if (!callbacks_enabled_ || destroyed_ || owner_ == nullptr) return false;
            ++callbacks_in_flight_;
        }
        const bool accepted = owner_->OnClientCallback(event);
        {
            std::lock_guard<std::mutex> lock(callback_mutex_);
            --callbacks_in_flight_;
        }
        callback_finished_.notify_all();
        return accepted;
    }

    int start_calls = 0;
    int close_calls = 0;
    int destroy_calls = 0;
    std::atomic<bool> destroy_entered{false};

private:
    rva::wss::WssOwner* owner_ = nullptr;
    std::mutex callback_mutex_;
    std::condition_variable callback_finished_;
    size_t callbacks_in_flight_ = 0;
    bool callbacks_enabled_ = false;
    bool destroyed_ = false;
};

void CountCallbackNotification(void* context) noexcept {
    ++*static_cast<size_t*>(context);
}

struct BlockingNotificationContext final {
    std::atomic<bool> alive{true};
    std::atomic<bool> entered{false};
    std::atomic<bool> release{false};
    std::atomic<size_t> notifications{0};
    std::atomic<size_t> accesses_after_retire{0};
};

void BlockAndCountCallbackNotification(void* context) noexcept {
    auto* notification = static_cast<BlockingNotificationContext*>(context);
    if (!notification->alive.load(std::memory_order_acquire)) {
        notification->accesses_after_retire.fetch_add(1, std::memory_order_relaxed);
    }
    notification->notifications.fetch_add(1, std::memory_order_relaxed);
    notification->entered.store(true, std::memory_order_release);
    while (!notification->release.load(std::memory_order_acquire)) {
        std::this_thread::yield();
    }
}

bool WaitUntilTrue(const std::atomic<bool>& value) {
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(2);
    while (std::chrono::steady_clock::now() < deadline) {
        if (value.load(std::memory_order_acquire)) return true;
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    return value.load(std::memory_order_acquire);
}

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
        R"("selected_media_profile":"wss-opus/1","audio":{"codec":"opus","sample_rate_hz":16000,)"
        R"("channels":1,"frame_duration_ms":60},"heartbeat_interval_ms":15000,"idle_timeout_ms":45000,)"
        R"("max_control_message_bytes":32768})";
    ServerMessage message;
    assert(rva::protocol::ParseServerMessage(
               reinterpret_cast<const uint8_t*>(opened_json.data()), opened_json.size(), &message) ==
           ControlError::kOk);
    const auto& opened = std::get<SessionOpened>(message);
    assert(opened.session.session_epoch == "epoch-007");
    assert(opened.media_epoch == 7);
    assert(opened.selected_media_profile == "wss-opus/1");
    assert(!opened.udp_grant.has_value());

    const std::string opened_udp_json =
        R"({"type":"session.opened","request_id":"open-002","session_id":"session-002",)"
        R"("session_epoch":"epoch-008","media_id":"fedcba9876543210","media_epoch":8,)"
        R"("selected_media_profile":"udp-opus-gcm/1","audio":{"codec":"opus","sample_rate_hz":16000,)"
        R"("channels":1,"frame_duration_ms":60},"heartbeat_interval_ms":15000,"idle_timeout_ms":45000,)"
        R"("max_control_message_bytes":32768,"udp_grant":{"host":"voice.example.test","port":8443,)"
        R"("expires_at_ms":1780000000000,"refresh_after_ms":595000,"uplink_key_b64":"AAAAAAAAAAAAAAAAAAAAAA==",)"
        R"("uplink_salt_b64":"AAAAAAAAAAA=","downlink_key_b64":"/////////////////////w==",)"
        R"("downlink_salt_b64":"//////////8=","probe_timeout_ms":1500}})";
    assert(rva::protocol::ParseServerMessage(
               reinterpret_cast<const uint8_t*>(opened_udp_json.data()), opened_udp_json.size(), &message) ==
           ControlError::kOk);
    const auto& opened_udp = std::get<SessionOpened>(message);
    assert(opened_udp.selected_media_profile == "udp-opus-gcm/1");
    assert(opened_udp.udp_grant.has_value());
    assert(opened_udp.udp_grant->host == "voice.example.test");
    assert(opened_udp.udp_grant->port == 8443);
    assert(opened_udp.udp_grant->expires_at_ms == 1780000000000ULL);
    assert(opened_udp.udp_grant->refresh_after_ms == 595000);
    assert(opened_udp.udp_grant->probe_timeout_ms == 1500);
    assert(opened_udp.udp_grant->uplink_key[0] == 0 && opened_udp.udp_grant->uplink_key[15] == 0);
    assert(opened_udp.udp_grant->downlink_key[0] == 0xff && opened_udp.udp_grant->downlink_key[15] == 0xff);

    const std::vector<std::string> rejected_opened_messages = {
        R"({"type":"session.opened","request_id":"open-001","session_id":"session-001","session_epoch":"epoch-007","media_id":"0123456789abcdef","media_epoch":7,"selected_media_profile":"udp-opus-gcm/1","audio":{"codec":"opus","sample_rate_hz":16000,"channels":1,"frame_duration_ms":60},"heartbeat_interval_ms":15000,"idle_timeout_ms":45000,"max_control_message_bytes":32768})",
        R"({"type":"session.opened","request_id":"open-001","session_id":"session-001","session_epoch":"epoch-007","media_id":"0123456789abcdef","media_epoch":7,"selected_media_profile":"wss-opus/1","audio":{"codec":"opus","sample_rate_hz":16000,"channels":1,"frame_duration_ms":60},"heartbeat_interval_ms":15000,"idle_timeout_ms":45000,"max_control_message_bytes":32768,"udp_grant":{}})",
        R"({"type":"session.opened","request_id":"open-002","session_id":"session-002","session_epoch":"epoch-008","media_id":"fedcba9876543210","media_epoch":8,"selected_media_profile":"udp-opus-gcm/1","audio":{"codec":"opus","sample_rate_hz":16000,"channels":1,"frame_duration_ms":60},"heartbeat_interval_ms":15000,"idle_timeout_ms":45000,"max_control_message_bytes":32768,"udp_grant":{"host":"voice.example.test","port":8443,"expires_at_ms":1780000000000,"uplink_key_b64":"AAAAAAAAAAAAAAAAAAAAAB==","uplink_salt_b64":"AAAAAAAAAAA=","downlink_key_b64":"/////////////////////w==","downlink_salt_b64":"//////////8=","probe_timeout_ms":1500}})",
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

    const std::vector<std::string> rejected_unregistered_messages = {
        R"({"type":"response.end","session_id":"session-001","session_epoch":"epoch-007","response_id":"resp-1","generation":3,"outcome":"completed"})",
        R"({"type":"response.end","session_id":"session-001","session_epoch":"epoch-007","response_id":"resp-1","generation":3,"outcome":"cancelled","final_media_sequence":1})",
        R"({"type":"response.end","session_id":"session-001","session_epoch":"epoch-007","response_id":"resp-1","generation":3,"outcome":"failed","error_code":"Bad-Code"})",
        R"({"type":"playback.stop","session_id":"session-001","session_epoch":"epoch-007","target":{"response_id":"resp-1","generation":3},"fence_generation":3,"cause":"recognized_interrupt"})",
        R"({"type":"response.cancelled","session_id":"session-001","session_epoch":"epoch-007","target":{"response_id":"resp-1","generation":3},"reason":"cancelled"})",
    };
    for (const std::string& rejected : rejected_unregistered_messages) {
        assert(rva::protocol::ParseServerMessage(
                   reinterpret_cast<const uint8_t*>(rejected.data()), rejected.size(), &message) !=
               ControlError::kOk);
    }

    rva::protocol::SessionOpen open;
    open.request_id = "open-001";
    open.device_id = "esp32s3-test";
    open.supported_media_profiles = {"wss-opus/1"};
    open.preferred_media_profile = "wss-opus/1";
    open.capabilities = {true, true, true, true, true};
    std::string encoded;
    assert(rva::protocol::EncodeSessionOpen(open, &encoded) == ControlError::kOk);
    cJSON* root = cJSON_ParseWithLength(encoded.data(), encoded.size());
    assert(root != nullptr);
    assert(std::strcmp(cJSON_GetStringValue(cJSON_GetObjectItem(root, "type")), "session.open") == 0);
    assert(cJSON_GetNumberValue(cJSON_GetObjectItem(root, "protocol_version")) == 1);
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

void TestDownlinkMediaSequenceSpansResponses() {
    rva::wss::WssSession session("open-001");
    SessionOpened opened;
    opened.request_id = "open-001";
    opened.session = {"session-001", "epoch-007"};
    opened.media_id = {0x01, 0x23, 0x45, 0x67, 0x89, 0xab, 0xcd, 0xef};
    opened.media_epoch = 7;
    assert(session.Accept(opened) == AdmissionResult::kAccepted);

    ResponseEvent response;
    response.type = ServerMessageType::kResponseBegin;
    response.session = opened.session;
    response.response_id = "resp-1";
    response.generation = 1;
    assert(session.Accept(response) == AdmissionResult::kAccepted);

    MediaHeader parsed;
    auto media = MakeMediaFrame(opened.media_id, opened.media_epoch, 0, 1);
    assert(session.AcceptMedia(media.data(), media.size(), &parsed) == AdmissionResult::kAccepted);
    response.type = ServerMessageType::kResponseEnd;
    response.outcome = rva::protocol::ResponseOutcome::kCompleted;
    assert(session.Accept(response) == AdmissionResult::kAccepted);

    response.type = ServerMessageType::kResponseBegin;
    response.response_id = "resp-2";
    response.generation = 2;
    assert(session.Accept(response) == AdmissionResult::kAccepted);
    media = MakeMediaFrame(opened.media_id, opened.media_epoch, 1, 2);
    assert(session.AcceptMedia(media.data(), media.size(), &parsed) == AdmissionResult::kAccepted);
    media = MakeMediaFrame(opened.media_id, opened.media_epoch, 0, 2);
    assert(session.AcceptMedia(media.data(), media.size(), &parsed) == AdmissionResult::kInvalidSequence);
}

void TestCallbackOnlyQueuesAndSupervisorOwnsTeardown() {
    FakeClient client;
    {
        rva::wss::WssOwner owner(client, 2, 6);
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

void TestCallbackQueueInitializationIsAllOrNothing() {
    using rva::wss::CallbackPayloadBuffer;

    assert(CallbackPayloadBuffer::outstanding_allocations_for_test() == 0);
    CallbackPayloadBuffer::SetAllocationFailureForTest(3);
    {
        rva::wss::CallbackEventQueue queue(4, 16);
        assert(!queue.ready());
        assert(queue.capacity() == 0);
        assert(CallbackPayloadBuffer::allocation_attempts_for_test() == 3);
        assert(CallbackPayloadBuffer::outstanding_allocations_for_test() == 0);
    }

    CallbackPayloadBuffer::SetAllocationFailureForTest(2);
    FakeClient client;
    {
        rva::wss::WssOwner owner(client, 4, 16);
        assert(!owner.Start());
        assert(client.start_calls == 0);
        assert(!owner.OnClientCallback({ClientEventType::kConnected}));
        assert(owner.dropped_events() == 1);
        assert(CallbackPayloadBuffer::allocation_attempts_for_test() == 2);
        assert(CallbackPayloadBuffer::outstanding_allocations_for_test() == 0);
    }
    assert(client.destroy_calls == 1);
    CallbackPayloadBuffer::SetAllocationFailureForTest(0);
    assert(CallbackPayloadBuffer::outstanding_allocations_for_test() == 0);
}

void TestCallbackBurstCapacityAndNotification() {
    static_assert(
        rva::wss::kMaximumCallbackEvents ==
        (rva::protocol::kMaxControlBytes + rva::wss::kMaximumCallbackFragmentBytes - 1) /
                rva::wss::kMaximumCallbackFragmentBytes +
            2);

    FakeClient client;
    rva::wss::WssOwner owner(
        client,
        rva::wss::kMaximumCallbackEvents,
        rva::wss::kMaximumQueuedCallbackBytes);
    size_t notifications = 0;
    owner.BindCallbackReadyNotifier(&CountCallbackNotification, &notifications);

    const std::array<uint8_t, 1> payload = {'x'};
    for (size_t index = 0; index < rva::wss::kMaximumCallbackEvents; ++index) {
        assert(owner.OnClientCallback(
            {ClientEventType::kTextFragment, payload.data(), payload.size(), 0, payload.size()}));
    }
    assert(owner.dropped_events() == 0);
    assert(notifications == rva::wss::kMaximumCallbackEvents);

    assert(!owner.OnClientCallback({ClientEventType::kConnected}));
    assert(owner.dropped_events() == 1);
    assert(notifications == rva::wss::kMaximumCallbackEvents + 1);

    rva::wss::OwnedClientEvent event;
    for (size_t index = 0; index < rva::wss::kMaximumCallbackEvents; ++index) {
        assert(owner.Poll(&event));
        assert(event.type == ClientEventType::kTextFragment);
        assert(event.data_size == payload.size());
        assert(event.data[0] == payload[0]);
    }
    assert(!owner.Poll(&event));
}

void TestSmallFragmentsCoalesceWithinControlFrameBudget() {
    constexpr size_t kFragmentSize = 1024;
    constexpr size_t kFragmentCount = rva::protocol::kMaxControlBytes / kFragmentSize;
    static_assert(kFragmentCount == 32);

    FakeClient client;
    rva::wss::WssOwner owner(
        client,
        rva::wss::kMaximumCallbackEvents,
        rva::wss::kMaximumQueuedCallbackBytes);
    std::array<uint8_t, kFragmentSize> fragment{};
    for (size_t index = 0; index < kFragmentCount; ++index) {
        fragment.fill(static_cast<uint8_t>(index));
        assert(owner.OnClientCallback({
            ClientEventType::kTextFragment,
            fragment.data(),
            fragment.size(),
            index * fragment.size(),
            rva::protocol::kMaxControlBytes,
        }));
    }
    assert(owner.OnClientCallback({ClientEventType::kDisconnected}));
    assert(owner.OnClientCallback({ClientEventType::kError}));
    assert(owner.dropped_events() == 0);
    assert(!owner.OnClientCallback({ClientEventType::kConnected}));
    assert(owner.dropped_events() == 1);

    rva::wss::FrameAssembler assembler;
    rva::wss::OwnedClientEvent event;
    std::vector<uint8_t> frame;
    constexpr size_t kDataDescriptors =
        rva::protocol::kMaxControlBytes / rva::wss::kMaximumCallbackFragmentBytes;
    for (size_t descriptor = 0; descriptor < kDataDescriptors; ++descriptor) {
        assert(owner.Poll(&event));
        assert(event.type == ClientEventType::kTextFragment);
        assert(event.payload_offset == descriptor * rva::wss::kMaximumCallbackFragmentBytes);
        assert(event.data_size == rva::wss::kMaximumCallbackFragmentBytes);
        const auto result = assembler.Consume(event, &frame);
        assert(result == (descriptor + 1 == kDataDescriptors
                              ? rva::wss::AssembleResult::kComplete
                              : rva::wss::AssembleResult::kIncomplete));
    }
    assert(frame.size() == rva::protocol::kMaxControlBytes);
    for (size_t index = 0; index < kFragmentCount; ++index) {
        for (size_t offset = 0; offset < kFragmentSize; ++offset) {
            assert(frame[index * kFragmentSize + offset] == static_cast<uint8_t>(index));
        }
    }
    assert(owner.Poll(&event));
    assert(event.type == ClientEventType::kDisconnected);
    assert(owner.Poll(&event));
    assert(event.type == ClientEventType::kError);
    assert(!owner.Poll(&event));
}

void TestCallbackNotifierTeardownBarrierContract() {
    BlockingCallbackClient client;
    rva::wss::WssOwner owner(client, 4, 16);
    client.BindEventSink(&owner);
    BlockingNotificationContext notification;
    owner.BindCallbackReadyNotifier(&BlockAndCountCallbackNotification, &notification);
    assert(owner.Start());

    std::atomic<bool> callback_result{false};
    std::thread callback([&]() {
        callback_result.store(
            client.Emit({ClientEventType::kConnected}), std::memory_order_release);
    });
    assert(WaitUntilTrue(notification.entered));
    assert(notification.notifications.load(std::memory_order_relaxed) == 1);

    std::atomic<bool> teardown_result{false};
    std::atomic<bool> teardown_finished{false};
    std::thread teardown([&]() {
        teardown_result.store(owner.SupervisorClose(100), std::memory_order_release);
        teardown_finished.store(true, std::memory_order_release);
    });
    assert(WaitUntilTrue(client.destroy_entered));
    assert(!teardown_finished.load(std::memory_order_acquire));

    notification.release.store(true, std::memory_order_release);
    callback.join();
    teardown.join();
    assert(callback_result.load(std::memory_order_acquire));
    assert(teardown_result.load(std::memory_order_acquire));
    assert(teardown_finished.load(std::memory_order_acquire));
    assert(client.close_calls == 1 && client.destroy_calls == 1);

    owner.BindCallbackReadyNotifier(nullptr, nullptr);
    notification.alive.store(false, std::memory_order_release);
    assert(owner.OnClientCallback({ClientEventType::kConnected}));
    assert(notification.notifications.load(std::memory_order_relaxed) == 1);
    assert(notification.accesses_after_retire.load(std::memory_order_relaxed) == 0);
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
    TestDownlinkMediaSequenceSpansResponses();
    TestCallbackOnlyQueuesAndSupervisorOwnsTeardown();
    TestCallbackQueueInitializationIsAllOrNothing();
    TestCallbackBurstCapacityAndNotification();
    TestSmallFragmentsCoalesceWithinControlFrameBudget();
    TestCallbackNotifierTeardownBarrierContract();
    TestTeardownFailureIsObservableAndRetryable();
    return 0;
}
