#include <cassert>

#include "native_runtime/response_interrupt_gate.h"

int main() {
    rva::runtime::ResponseInterruptGate gate;
    rva::protocol::CancelTarget target;

    assert(!gate.active());
    assert(!gate.PrepareCancel(&target));
    assert(!gate.PrepareCancel(nullptr));

    gate.Begin("response-1", 7);
    assert(gate.active());
    assert(gate.PrepareCancel(&target));
    assert(target.response_id == "response-1");
    assert(target.generation == 7);
    assert(!gate.PrepareCancel(&target));

    gate.End();
    assert(!gate.active());
    assert(!gate.PrepareCancel(&target));

    gate.Begin("response-2", 8);
    assert(gate.PrepareCancel(&target));
    assert(target.response_id == "response-2");
    assert(target.generation == 8);
    return 0;
}
