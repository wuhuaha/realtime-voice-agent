#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <string>
#include <utility>

#include "voice_protocol/control.h"
#include "voice_protocol/media_header.h"

namespace rva::wss {

enum class AdmissionResult {
    kAccepted,
    kSessionNotOpen,
    kStaleSession,
    kStaleGeneration,
    kInvalidSequence,
    kCancelTargetMismatch,
    kResponseAlreadyActive,
    kResponseAlreadyTerminal,
    kPlaybackStopConflict,
    kStaleMediaIdentity,
    kInvalidMedia,
    kSessionClosed,
    kUnexpectedMessage,
};

class WssSession final {
public:
    explicit WssSession(std::string open_request_id) : open_request_id_(std::move(open_request_id)) {}

    AdmissionResult Accept(const protocol::ServerMessage& message);
    AdmissionResult AcceptMedia(const uint8_t* frame, size_t size, protocol::MediaHeader* header);
    protocol::ControlError EncodeCancelRequest(
        const std::string& request_id, std::string* json) const;

    bool opened() const { return opened_; }
    bool closed() const { return closed_; }
    uint32_t playback_fence() const { return playback_fence_; }

private:
    enum class PlaybackStopCause : uint8_t {
        kNone,
        kExplicitUserRequest,
        kRecognizedInterrupt,
        kSessionClose,
        kResponseFailed,
    };

    static PlaybackStopCause ParsePlaybackStopCause(const std::string& cause);
    bool SessionMatches(const protocol::SessionIdentity& session) const;
    AdmissionResult AcceptOpened(const protocol::SessionOpened& opened);
    AdmissionResult AcceptTranscript(const protocol::Transcript& transcript);
    AdmissionResult AcceptResponse(const protocol::ResponseEvent& response);
    AdmissionResult AcceptPlaybackStop(const protocol::PlaybackStop& stop);

    const std::string open_request_id_;
    protocol::SessionIdentity session_;
    std::array<uint8_t, 8> media_id_{};
    uint32_t media_epoch_ = 0;
    uint32_t playback_fence_ = 0;
    uint32_t next_media_sequence_ = 0;
    uint32_t next_transcript_sequence_ = 0;
    uint32_t next_response_text_sequence_ = 0;
    std::string utterance_id_;
    std::string response_id_;
    uint32_t response_generation_ = 0;
    uint32_t last_stop_fence_generation_ = 0;
    PlaybackStopCause last_stop_cause_ = PlaybackStopCause::kNone;
    bool opened_ = false;
    bool closed_ = false;
    bool utterance_active_ = false;
    bool response_active_ = false;
    bool media_active_ = false;
    bool stop_recorded_ = false;
};

}  // namespace rva::wss
