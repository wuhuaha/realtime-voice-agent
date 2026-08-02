#include <cassert>
#include <optional>

#include "native_runtime/playback_state.h"

int main() {
    using rva::protocol::PlaybackEndedOutcome;
    using rva::protocol::ResponseTarget;
    using rva::runtime::PlaybackFact;
    using rva::runtime::PlaybackFactType;
    using rva::runtime::PlaybackState;

    PlaybackState state;
    std::optional<PlaybackFact> fact;
    const ResponseTarget first{"response-1", 3};
    assert(state.Begin(first));
    assert(state.CanPlay(3));
    assert(!state.CanPlay(2));
    assert(!state.Begin({"response-duplicate", 4}));
    assert(!state.SetFinalMediaSequence({"other", 3}, 1, 100, &fact));
    assert(state.SetFinalMediaSequence(first, 1, 100, &fact));
    assert(!fact.has_value());

    assert(state.RecordWritten(0, 240, &fact));
    assert(fact && fact->type == PlaybackFactType::kStarted);
    assert(fact->target.response_id == "response-1");
    assert(fact->first_media_sequence == 0);
    assert(state.RecordWritten(0, 1200, &fact));
    assert(!fact.has_value());
    assert(state.FinishFrame(0, &fact));
    assert(!fact.has_value());
    assert(state.RecordWritten(1, 1440, &fact));
    assert(!fact.has_value());
    assert(state.FinishFrame(1, &fact));
    assert(fact && fact->type == PlaybackFactType::kEnded);
    assert(fact->outcome == PlaybackEndedOutcome::kCompleted);
    assert(fact->played_samples == 2880);
    assert(fact->last_media_sequence == 1);
    assert(!state.active());

    const ResponseTarget second{"response-2", 4};
    assert(state.Begin(second));
    assert(state.RecordWritten(10, 240, &fact));
    assert(fact && fact->type == PlaybackFactType::kStarted);
    assert(!state.Stop(second, 4, &fact));
    assert(state.Stop(second, 5, &fact));
    assert(fact && fact->outcome == PlaybackEndedOutcome::kStopped);
    assert(fact->played_samples == 240);
    assert(fact->last_media_sequence == 10);
    assert(state.Stop(second, 5, &fact));
    assert(!fact.has_value());
    assert(!state.Stop(second, 6, &fact));
    assert(!fact.has_value());
    assert(!state.Begin({"response-stale", 5}));

    const ResponseTarget third{"response-3", 6};
    assert(state.Begin(third));
    assert(state.Stop(third, 7, &fact));
    assert(fact && fact->played_samples == 0);
    assert(!fact->last_media_sequence.has_value());

    const ResponseTarget fourth{"response-4", 8};
    assert(state.Begin(fourth));
    assert(state.RecordWritten(20, 240, &fact));
    assert(state.Fail(&fact));
    assert(fact && fact->outcome == PlaybackEndedOutcome::kFailed);
    assert(fact->played_samples == 240);

    const ResponseTarget degraded{"response-degraded", 9};
    assert(state.Begin(degraded));
    assert(state.RecordWritten(30, 960, &fact));
    assert(state.FinishFrame(30, &fact));
    assert(state.MarkDegraded());
    assert(state.SetFinalMediaSequence(degraded, 30, 1'000, &fact));
    assert(fact && fact->outcome == PlaybackEndedOutcome::kFailed);

    const ResponseTarget final_plc{"response-final-plc", 10};
    assert(state.Begin(final_plc));
    assert(state.RecordWritten(40, 960, &fact));
    assert(state.FinishFrame(40, &fact));
    assert(state.SetFinalMediaSequence(final_plc, 41, 2'000, &fact));
    uint32_t plc_sequence = 0;
    assert(!state.RequestFinalPlc(
        2'000 + PlaybackState::kDrainDeadlineUs - 1, &plc_sequence));
    assert(state.RequestFinalPlc(
        2'000 + PlaybackState::kDrainDeadlineUs, &plc_sequence));
    assert(plc_sequence == 41);
    assert(!state.RequestFinalPlc(
        2'000 + PlaybackState::kDrainDeadlineUs, &plc_sequence));
    assert(state.RecordWritten(41, 960, &fact));
    assert(state.FinishFrame(41, &fact));
    assert(fact && fact->outcome == PlaybackEndedOutcome::kCompleted);

    const ResponseTarget missing_many{"response-missing-many", 11};
    assert(state.Begin(missing_many));
    assert(state.RecordWritten(50, 960, &fact));
    assert(state.FinishFrame(50, &fact));
    assert(state.SetFinalMediaSequence(missing_many, 52, 3'000, &fact));
    assert(!state.RequestFinalPlc(
        3'000 + PlaybackState::kDrainDeadlineUs, &plc_sequence));
    assert(state.ExpireDrain(
        3'000 + PlaybackState::kDrainDeadlineUs, &fact));
    assert(fact && fact->outcome == PlaybackEndedOutcome::kFailed);
    assert(fact->last_media_sequence == 50);

    const ResponseTarget never_started{"response-never-started", 12};
    assert(state.Begin(never_started));
    assert(state.SetFinalMediaSequence(never_started, 60, 4'000, &fact));
    assert(state.ExpireDrain(
        4'000 + PlaybackState::kDrainDeadlineUs, &fact));
    assert(fact && fact->outcome == PlaybackEndedOutcome::kFailed);
    assert(fact->played_samples == 0);
    assert(!fact->last_media_sequence.has_value());

    state.Reset();
    assert(!state.active());
    assert(state.fence_generation() == 0);
    return 0;
}
