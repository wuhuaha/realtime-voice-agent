#include "audio_pipeline/audio_pipeline.h"

namespace rva::audio {
namespace {

PipelineResult Failure(PipelineStage stage, PortResult error) {
    return {.stage = stage, .error = error};
}

}  // namespace

AudioPipeline::AudioPipeline(CapturePort& capture, FrontendPort& frontend, PlaybackPort& playback)
    : capture_(capture), frontend_(frontend), playback_(playback) {}

AudioPipeline::~AudioPipeline() {
    Stop();
}

PipelineResult AudioPipeline::Start() {
    PipelineState expected = PipelineState::kStopped;
    if (!state_.compare_exchange_strong(expected, PipelineState::kStarting)) {
        if (expected == PipelineState::kRunning) {
            return PipelineResult::Ok();
        }
        return Failure(PipelineStage::kPipeline, PortResult::kInvalidState);
    }

    last_cleanup_error_ = PipelineResult::Ok();
    PortResult result = playback_.Start();
    if (result != PortResult::kOk) {
        last_cleanup_error_ = StopStartedStages(false, false, true);
        state_.store(last_cleanup_error_.ok() ? PipelineState::kStopped : PipelineState::kFaulted);
        return Failure(PipelineStage::kPlayback, result);
    }

    result = frontend_.Start();
    if (result != PortResult::kOk) {
        last_cleanup_error_ = StopStartedStages(false, true, true);
        state_.store(last_cleanup_error_.ok() ? PipelineState::kStopped : PipelineState::kFaulted);
        return Failure(PipelineStage::kFrontend, result);
    }

    result = capture_.Start();
    if (result != PortResult::kOk) {
        last_cleanup_error_ = StopStartedStages(true, true, true);
        state_.store(last_cleanup_error_.ok() ? PipelineState::kStopped : PipelineState::kFaulted);
        return Failure(PipelineStage::kCapture, result);
    }

    state_.store(PipelineState::kRunning);
    return PipelineResult::Ok();
}

PipelineResult AudioPipeline::Stop() {
    const PipelineState prior = state_.exchange(PipelineState::kStopping);
    if (prior == PipelineState::kStopped) {
        state_.store(PipelineState::kStopped);
        return PipelineResult::Ok();
    }
    if (prior == PipelineState::kStarting || prior == PipelineState::kStopping) {
        state_.store(prior);
        return Failure(PipelineStage::kPipeline, PortResult::kInvalidState);
    }

    last_cleanup_error_ = StopStartedStages(true, true, true);
    state_.store(last_cleanup_error_.ok() ? PipelineState::kStopped : PipelineState::kFaulted);
    return last_cleanup_error_;
}

PortResult AudioPipeline::ReadCapture(MutablePcmView* destination, uint32_t timeout_ms) {
    if (!IsRunning()) {
        return PortResult::kInvalidState;
    }
    return capture_.Read(destination, timeout_ms);
}

PortResult AudioPipeline::FeedFrontend(PcmView input) {
    if (!IsRunning()) {
        return PortResult::kInvalidState;
    }
    return frontend_.Feed(input);
}

PortResult AudioPipeline::FetchFrontend(MutablePcmView* output, uint32_t timeout_ms) {
    if (!IsRunning()) {
        return PortResult::kInvalidState;
    }
    return frontend_.Fetch(output, timeout_ms);
}

PortResult AudioPipeline::WritePlayback(PcmView input, uint32_t timeout_ms) {
    if (!IsRunning()) {
        return PortResult::kInvalidState;
    }
    return playback_.Write(input, timeout_ms);
}

PipelineResult AudioPipeline::StopStartedStages(bool capture_started, bool frontend_started,
                                                bool playback_started) {
    PipelineResult first_error = PipelineResult::Ok();
    const auto record = [&first_error](PipelineStage stage, PortResult result) {
        if (first_error.ok() && result != PortResult::kOk) {
            first_error = Failure(stage, result);
        }
    };

    if (capture_started) {
        record(PipelineStage::kCapture, capture_.Stop());
    }
    if (frontend_started) {
        record(PipelineStage::kFrontend, frontend_.Stop());
    }
    if (playback_started) {
        record(PipelineStage::kPlayback, playback_.Stop());
    }
    return first_error;
}

bool AudioPipeline::IsRunning() const {
    return state_.load() == PipelineState::kRunning;
}

}  // namespace rva::audio
