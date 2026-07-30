from __future__ import annotations

import asyncio
from collections.abc import Awaitable

import pytest
from realtime_worker.agent import AgentOutputSegment, DeterministicAgentRunner, LiveKitAgentRunner, PcmFrame
from realtime_worker.config import Settings


async def discard(segment: AgentOutputSegment) -> None:
    return None


class _Session:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.forces: list[bool] = []

    def interrupt(self, *, force: bool = False) -> Awaitable[None]:
        self.forces.append(force)
        future = asyncio.get_running_loop().create_future()
        if self.error is None:
            future.set_result(None)
        else:
            future.set_exception(self.error)
        return future


@pytest.mark.asyncio
async def test_livekit_runner_uses_public_forced_interrupt_without_replacing_session() -> None:
    runner = LiveKitAgentRunner(
        Settings(runner="livekit", deepseek_api_key="test", lab_token="test-token"),
        discard,
        lambda _epoch: None,
    )
    session = _Session()
    runner._session = session  # type: ignore[assignment]  # noqa: SLF001

    assert await runner.interrupt() == 3
    assert await runner.interrupt() == 5

    assert runner._session is session  # noqa: SLF001
    assert session.forces == [True, True]


@pytest.mark.asyncio
async def test_interrupt_failure_is_not_hidden() -> None:
    runner = LiveKitAgentRunner(
        Settings(runner="livekit", deepseek_api_key="test", lab_token="test-token"),
        discard,
        lambda _epoch: None,
    )
    runner._session = _Session(error=RuntimeError("interrupt failed"))  # type: ignore[assignment]  # noqa: SLF001
    with pytest.raises(RuntimeError, match="interrupt failed"):
        await runner.interrupt()


@pytest.mark.asyncio
async def test_deterministic_interrupt_is_repeatable() -> None:
    runner = DeterministicAgentRunner(discard)
    assert await runner.interrupt() == 2
    assert await runner.interrupt() == 3


@pytest.mark.asyncio
async def test_deterministic_runner_emits_repeatable_transcript_before_response() -> None:
    transcripts: list[tuple[str, bool]] = []
    responses: list[str] = []
    runner = DeterministicAgentRunner(discard)
    runner.set_text_sinks(
        lambda text, is_final: transcripts.append((text, is_final)),
        responses.append,
    )
    frame = PcmFrame(0, 0, 0, bytes(640))

    for _ in range(3):
        await runner.push_audio(frame)
    await asyncio.sleep(0)

    assert transcripts == [("deterministic", False), ("deterministic turn", True)]
    assert responses == ["deterministic turn"]
