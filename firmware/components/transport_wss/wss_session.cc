#include "transport_wss/wss_session.h"

#include <type_traits>
#include <variant>

namespace rva::wss {

WssSession::PlaybackStopCause WssSession::ParsePlaybackStopCause(
    const std::string& cause) {
    if (cause == "explicit_user_request") {
        return WssSession::PlaybackStopCause::kExplicitUserRequest;
    }
    if (cause == "recognized_interrupt") {
        return WssSession::PlaybackStopCause::kRecognizedInterrupt;
    }
    if (cause == "session_close") {
        return WssSession::PlaybackStopCause::kSessionClose;
    }
    if (cause == "response_failed") {
        return WssSession::PlaybackStopCause::kResponseFailed;
    }
    return WssSession::PlaybackStopCause::kNone;
}

bool WssSession::SessionMatches(const protocol::SessionIdentity& session) const {
    return opened_ && session.session_id == session_.session_id && session.session_epoch == session_.session_epoch;
}

AdmissionResult WssSession::Accept(const protocol::ServerMessage& message) {
    if (closed_) return AdmissionResult::kSessionClosed;
    return std::visit(
        [this](const auto& typed) -> AdmissionResult {
            using Type = std::decay_t<decltype(typed)>;
            if constexpr (std::is_same_v<Type, protocol::SessionOpened>) {
                return AcceptOpened(typed);
            } else if constexpr (std::is_same_v<Type, protocol::Transcript>) {
                return AcceptTranscript(typed);
            } else if constexpr (std::is_same_v<Type, protocol::ResponseEvent>) {
                return AcceptResponse(typed);
            } else if constexpr (std::is_same_v<Type, protocol::PlaybackStop>) {
                return AcceptPlaybackStop(typed);
            } else if constexpr (std::is_same_v<Type, protocol::SessionError>) {
                return !opened_ ? AdmissionResult::kSessionNotOpen
                                : (SessionMatches(typed.session) ? AdmissionResult::kAccepted
                                                                 : AdmissionResult::kStaleSession);
            } else {
                if (!opened_) return AdmissionResult::kSessionNotOpen;
                if (!SessionMatches(typed.session)) return AdmissionResult::kStaleSession;
                closed_ = true;
                response_active_ = false;
                media_active_ = false;
                utterance_active_ = false;
                return AdmissionResult::kAccepted;
            }
        },
        message);
}

AdmissionResult WssSession::AcceptOpened(const protocol::SessionOpened& opened) {
    if (opened_) return AdmissionResult::kUnexpectedMessage;
    if (opened.request_id != open_request_id_) return AdmissionResult::kStaleSession;
    session_ = opened.session;
    media_id_ = opened.media_id;
    media_epoch_ = opened.media_epoch;
    opened_ = true;
    return AdmissionResult::kAccepted;
}

AdmissionResult WssSession::AcceptTranscript(const protocol::Transcript& transcript) {
    if (!opened_) return AdmissionResult::kSessionNotOpen;
    if (!SessionMatches(transcript.session)) return AdmissionResult::kStaleSession;
    if (!utterance_active_) {
        if (transcript.sequence != 0) return AdmissionResult::kInvalidSequence;
        utterance_id_ = transcript.utterance_id;
        next_transcript_sequence_ = 0;
        utterance_active_ = true;
    }
    if (transcript.utterance_id != utterance_id_ || transcript.sequence != next_transcript_sequence_) {
        return AdmissionResult::kInvalidSequence;
    }
    ++next_transcript_sequence_;
    if (transcript.final) {
        utterance_active_ = false;
        utterance_id_.clear();
    }
    return AdmissionResult::kAccepted;
}

AdmissionResult WssSession::AcceptResponse(const protocol::ResponseEvent& response) {
    if (!opened_) return AdmissionResult::kSessionNotOpen;
    if (!SessionMatches(response.session)) return AdmissionResult::kStaleSession;
    if (response.type == protocol::ServerMessageType::kResponseBegin) {
        if (response_active_) return AdmissionResult::kResponseAlreadyActive;
        if (response.generation <= playback_fence_) return AdmissionResult::kStaleGeneration;
        playback_fence_ = response.generation;
        response_generation_ = response.generation;
        response_id_ = response.response_id;
        next_response_text_sequence_ = 0;
        response_active_ = true;
        media_active_ = true;
        stop_recorded_ = false;
        last_stop_fence_generation_ = 0;
        last_stop_cause_ = PlaybackStopCause::kNone;
        return AdmissionResult::kAccepted;
    }
    if (!response_active_) return AdmissionResult::kResponseAlreadyTerminal;
    if (response.response_id != response_id_ || response.generation != response_generation_) {
        return response.generation < playback_fence_ ? AdmissionResult::kStaleGeneration
                                                     : AdmissionResult::kCancelTargetMismatch;
    }
    if (response.type == protocol::ServerMessageType::kResponseText) {
        if (response.sequence != next_response_text_sequence_) return AdmissionResult::kInvalidSequence;
        ++next_response_text_sequence_;
        return AdmissionResult::kAccepted;
    }
    if (response.type == protocol::ServerMessageType::kResponseEnd) {
        response_active_ = false;
        media_active_ = false;
        return AdmissionResult::kAccepted;
    }
    return AdmissionResult::kUnexpectedMessage;
}

AdmissionResult WssSession::AcceptPlaybackStop(const protocol::PlaybackStop& stop) {
    if (!opened_) return AdmissionResult::kSessionNotOpen;
    if (!SessionMatches(stop.session)) return AdmissionResult::kStaleSession;
    if (stop.target.response_id != response_id_ ||
        stop.target.generation != response_generation_) {
        return stop.target.generation < playback_fence_
                   ? AdmissionResult::kStaleGeneration
                   : AdmissionResult::kCancelTargetMismatch;
    }
    const PlaybackStopCause cause = ParsePlaybackStopCause(stop.cause);
    if (cause == PlaybackStopCause::kNone) {
        return AdmissionResult::kUnexpectedMessage;
    }
    if (stop_recorded_) {
        return stop.fence_generation == last_stop_fence_generation_ &&
                       cause == last_stop_cause_
                   ? AdmissionResult::kAccepted
                   : AdmissionResult::kPlaybackStopConflict;
    }
    if (stop.fence_generation <= playback_fence_) {
        return AdmissionResult::kStaleGeneration;
    }
    playback_fence_ = stop.fence_generation;
    last_stop_fence_generation_ = stop.fence_generation;
    last_stop_cause_ = cause;
    stop_recorded_ = true;
    media_active_ = false;
    return AdmissionResult::kAccepted;
}

AdmissionResult WssSession::AcceptMedia(
    const uint8_t* frame,
    size_t size,
    protocol::MediaHeader* header) {
    if (closed_) return AdmissionResult::kSessionClosed;
    if (!opened_) return AdmissionResult::kSessionNotOpen;
    protocol::MediaHeader parsed;
    if (protocol::ParseMediaHeader(frame, size, protocol::MediaDirection::kDownlink, &parsed) !=
        protocol::MediaError::kOk || parsed.flags != 0x01) {
        return AdmissionResult::kInvalidMedia;
    }
    if (parsed.media_id != media_id_ || parsed.media_epoch != media_epoch_) {
        return AdmissionResult::kStaleMediaIdentity;
    }
    if (parsed.generation < playback_fence_ || !media_active_ ||
        parsed.generation != response_generation_) {
        return AdmissionResult::kStaleGeneration;
    }
    if (parsed.sequence != next_media_sequence_) return AdmissionResult::kInvalidSequence;
    ++next_media_sequence_;
    if (header != nullptr) *header = parsed;
    return AdmissionResult::kAccepted;
}

protocol::ControlError WssSession::EncodeCancelRequest(
    const std::string& request_id,
    std::string* json) const {
    if (!opened_ || closed_ || response_id_.empty() || response_generation_ == 0) {
        return protocol::ControlError::kMissingOrInvalidField;
    }
    return protocol::EncodeResponseCancelRequest(
        {.session = session_,
         .request_id = request_id,
         .target = {response_id_, response_generation_}},
        json);
}

}  // namespace rva::wss
