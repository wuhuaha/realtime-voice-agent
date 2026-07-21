#include <cassert>
#include <vector>

#include "audio_pipeline/audio_pipeline.h"

namespace {

using rva::audio::CapturePort;
using rva::audio::FrontendPort;
using rva::audio::MutablePcmView;
using rva::audio::PcmView;
using rva::audio::PipelineStage;
using rva::audio::PipelineState;
using rva::audio::PlaybackPort;
using rva::audio::PortResult;

struct Events {
    std::vector<int> values;
};

class Capture final : public CapturePort {
public:
    explicit Capture(Events& events) : events_(events) {}
    PortResult start_result = PortResult::kOk;
    PortResult stop_result = PortResult::kOk;
    PortResult Start() override { events_.values.push_back(5); return start_result; }
    PortResult Stop() override { events_.values.push_back(6); return stop_result; }
    PortResult Read(MutablePcmView*, uint32_t) override { return PortResult::kOk; }
private:
    Events& events_;
};

class Frontend final : public FrontendPort {
public:
    explicit Frontend(Events& events) : events_(events) {}
    PortResult start_result = PortResult::kOk;
    PortResult stop_result = PortResult::kOk;
    PortResult Start() override { events_.values.push_back(3); return start_result; }
    PortResult Stop() override { events_.values.push_back(7); return stop_result; }
    PortResult Feed(PcmView) override { return PortResult::kOk; }
    PortResult Fetch(MutablePcmView*, uint32_t) override { return PortResult::kOk; }
private:
    Events& events_;
};

class Playback final : public PlaybackPort {
public:
    explicit Playback(Events& events) : events_(events) {}
    PortResult start_result = PortResult::kOk;
    PortResult stop_result = PortResult::kOk;
    PortResult Start() override { events_.values.push_back(1); return start_result; }
    PortResult Stop() override { events_.values.push_back(9); return stop_result; }
    PortResult Write(PcmView, uint32_t) override { return PortResult::kOk; }
private:
    Events& events_;
};

void TestLifecycleOrderAndIdempotence() {
    Events events;
    Capture capture(events);
    Frontend frontend(events);
    Playback playback(events);
    rva::audio::AudioPipeline pipeline(capture, frontend, playback);

    assert(pipeline.Start().ok());
    assert(pipeline.Start().ok());
    assert(pipeline.state() == PipelineState::kRunning);
    assert(pipeline.Stop().ok());
    assert(pipeline.Stop().ok());
    assert((events.values == std::vector<int>{1, 3, 5, 6, 7, 9}));
}

void TestStartRollback() {
    Events events;
    Capture capture(events);
    Frontend frontend(events);
    Playback playback(events);
    capture.start_result = PortResult::kIoFailure;
    rva::audio::AudioPipeline pipeline(capture, frontend, playback);

    const auto result = pipeline.Start();
    assert(result.stage == PipelineStage::kCapture);
    assert(result.error == PortResult::kIoFailure);
    assert(pipeline.state() == PipelineState::kStopped);
    assert((events.values == std::vector<int>{1, 3, 5, 6, 7, 9}));
}

void TestCleanupFailureFaultsPipeline() {
    Events events;
    Capture capture(events);
    Frontend frontend(events);
    Playback playback(events);
    frontend.stop_result = PortResult::kTimeout;
    rva::audio::AudioPipeline pipeline(capture, frontend, playback);

    assert(pipeline.Start().ok());
    const auto result = pipeline.Stop();
    assert(result.stage == PipelineStage::kFrontend);
    assert(pipeline.state() == PipelineState::kFaulted);
}

}  // namespace

int main() {
    TestLifecycleOrderAndIdempotence();
    TestStartRollback();
    TestCleanupFailureFaultsPipeline();
    return 0;
}
