from __future__ import annotations

import asyncio

import pytest
from realtime_worker.interruption import (
    InterruptionContext,
    InterruptionCoordinator,
    InterruptionPolicyConfig,
    LayeredInterruptionPolicy,
)

pytestmark = pytest.mark.unit


def context(
    transcript: str,
    *,
    is_final: bool = True,
    playback_age_seconds: float = 1.0,
    candidate_age_seconds: float = 0.5,
    assistant_text: str = "",
) -> InterruptionContext:
    return InterruptionContext(
        transcript=transcript,
        is_final=is_final,
        playback_age_seconds=playback_age_seconds,
        candidate_age_seconds=candidate_age_seconds,
        assistant_text=assistant_text,
    )


@pytest.mark.asyncio
async def test_exact_explicit_interrupt_command_bypasses_playback_guard() -> None:
    policy = LayeredInterruptionPolicy(InterruptionPolicyConfig())

    result = await policy.evaluate(context("停一下", playback_age_seconds=0.1, candidate_age_seconds=0.1))

    assert result.decision == "interrupt"
    assert result.reason == "explicit_interrupt_phrase"


@pytest.mark.asyncio
@pytest.mark.parametrize("phrase", ["停止", "停一下", "请停止", "重新说", "取消回答"])
async def test_bounded_command_grammar_interrupts_playback(phrase: str) -> None:
    policy = LayeredInterruptionPolicy(InterruptionPolicyConfig())

    result = await policy.evaluate(context(phrase, playback_age_seconds=0.1, candidate_age_seconds=0.1))

    assert result.decision == "interrupt"
    assert result.reason == "explicit_interrupt_phrase"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transcript",
    [
        "请你不要停止介绍这个方案",
        "这个方案不对吗",
        "我问的是天气为什么变化",
        "先别说这个功能不好",
        "重新说明一下配置有什么影响",
    ],
)
async def test_command_words_embedded_in_normal_speech_do_not_interrupt(transcript: str) -> None:
    policy = LayeredInterruptionPolicy(InterruptionPolicyConfig())

    result = await policy.evaluate(context(transcript))

    assert result.decision != "interrupt"


@pytest.mark.asyncio
async def test_short_backchannel_does_not_interrupt_playback() -> None:
    policy = LayeredInterruptionPolicy(InterruptionPolicyConfig())

    result = await policy.evaluate(context("嗯嗯", playback_age_seconds=1.5, candidate_age_seconds=1.0))

    assert result.decision == "backchannel"


@pytest.mark.asyncio
async def test_text_similar_to_assistant_is_treated_as_echo() -> None:
    policy = LayeredInterruptionPolicy(InterruptionPolicyConfig())

    result = await policy.evaluate(
        context(
            "今天天气很好",
            playback_age_seconds=1.5,
            candidate_age_seconds=1.0,
            assistant_text="今天天气很好，我建议你出去走走。",
        )
    )

    assert result.decision == "echo_or_noise"


@pytest.mark.asyncio
async def test_non_final_ambiguous_text_defers() -> None:
    policy = LayeredInterruptionPolicy(InterruptionPolicyConfig())

    result = await policy.evaluate(context("我想问另一个问题", is_final=False))

    assert result.decision == "defer"


@pytest.mark.asyncio
async def test_interruption_coordinator_is_single_flight_and_latest_wins() -> None:
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    evaluated: list[str] = []
    interrupted: list[str] = []

    class BlockingPolicy:
        async def evaluate(self, candidate: InterruptionContext):
            evaluated.append(candidate.transcript)
            if len(evaluated) == 1:
                first_started.set()
                await release_first.wait()
            from realtime_worker.interruption import InterruptionResult

            return InterruptionResult("interrupt", "explicit_interrupt_phrase")

    async def interrupt(candidate: InterruptionContext) -> None:
        interrupted.append(candidate.transcript)

    coordinator = InterruptionCoordinator(BlockingPolicy(), interrupt)
    coordinator.submit(context("停", is_final=False))
    await first_started.wait()
    coordinator.submit(context("停止", is_final=False))
    coordinator.submit(context("停止。", is_final=True))
    release_first.set()
    await coordinator.wait_idle()

    assert evaluated == ["停", "停止。"]
    assert interrupted == ["停止。"]
