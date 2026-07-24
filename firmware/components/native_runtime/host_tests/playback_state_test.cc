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
    assert(!state.SetFinalMediaSequence({"other", 3}, 1, &fact));
    assert(state.SetFinalMediaSequence(first, 1, &fact));
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

    state.Reset();
    assert(!state.active());
    assert(state.fence_generation() == 0);
    return 0;
}
