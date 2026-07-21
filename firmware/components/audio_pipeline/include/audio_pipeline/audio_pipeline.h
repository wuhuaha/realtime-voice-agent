#pragma once

#include <atomic>
#include <cstdint>

#include "audio_pipeline/audio_ports.h"

namespace rva::audio {

enum class PipelineState : uint8_t {
    kStopped = 0,
    kStarting,
    kRunning,
    kStopping,
    kFaulted,
};

enum class PipelineStage : uint8_t {
    kNone = 0,
    kPlayback,
    kFrontend,
    kCapture,
    kPipeline,
};

struct PipelineResult final {
    PipelineStage stage = PipelineStage::kNone;
    PortResult error = PortResult::kOk;

    [[nodiscard]] bool ok() const { return error == PortResult::kOk; }
    static constexpr PipelineResult Ok() { return {}; }
};

// Owns stage lifecycle, not the port objects. The application owns the tasks that
// call Read/Feed/Fetch/Write and must join them before destroying this object.
class AudioPipeline final {
public:
    AudioPipeline(CapturePort& capture, FrontendPort& frontend, PlaybackPort& playback);
    ~AudioPipeline();

    AudioPipeline(const AudioPipeline&) = delete;
    AudioPipeline& operator=(const AudioPipeline&) = delete;

    PipelineResult Start();
    PipelineResult Stop();

    PortResult ReadCapture(MutablePcmView* destination, uint32_t timeout_ms);
    PortResult FeedFrontend(PcmView input);
    PortResult FetchFrontend(MutablePcmView* output, uint32_t timeout_ms);
    PortResult WritePlayback(PcmView input, uint32_t timeout_ms);

    [[nodiscard]] PipelineState state() const { return state_.load(); }
    [[nodiscard]] PipelineResult last_cleanup_error() const { return last_cleanup_error_; }

private:
    PipelineResult StopStartedStages(bool capture_started, bool frontend_started,
                                     bool playback_started);
    [[nodiscard]] bool IsRunning() const;

    CapturePort& capture_;
    FrontendPort& frontend_;
    PlaybackPort& playback_;
    std::atomic<PipelineState> state_{PipelineState::kStopped};
    PipelineResult last_cleanup_error_{};
};

}  // namespace rva::audio
