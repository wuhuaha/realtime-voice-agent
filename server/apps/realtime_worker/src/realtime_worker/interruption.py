"""Server-authoritative playback interruption policy and serialization."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol

InterruptionDecision = Literal["interrupt", "backchannel", "echo_or_noise", "ignore", "defer"]


@dataclass(frozen=True, slots=True)
class InterruptionContext:
    transcript: str
    is_final: bool
    playback_age_seconds: float
    candidate_age_seconds: float
    assistant_text: str
    response_id: str = ""
    generation: int = 0


@dataclass(frozen=True, slots=True)
class InterruptionResult:
    decision: InterruptionDecision
    reason: str
    confidence: float = 1.0

    @property
    def should_interrupt(self) -> bool:
        return self.decision == "interrupt"


@dataclass(frozen=True, slots=True)
class InterruptionPolicyConfig:
    enabled: bool = True


class InterruptionPolicy(Protocol):
    async def evaluate(self, context: InterruptionContext) -> InterruptionResult: ...


_PUNCTUATION = re.compile(r"[\s，。！？、,.!?；;：:（）()【】\[\]\"'“”‘’]+")
_BACKCHANNELS = frozenset({"嗯", "嗯嗯", "哦", "噢", "啊", "好", "好的", "对", "是", "可以", "行", "知道了", "继续"})

# This is deliberately a finite grammar. A command-looking token embedded in a
# normal sentence must never become a playback side effect.
_INTERRUPT_COMMANDS = frozenset(
    {
        "停",
        "停止",
        "停一下",
        "暂停",
        "等一下",
        "等等",
        "先停一下",
        "请停止",
        "请停一下",
        "别说了",
        "先别说了",
        "不要说了",
        "取消",
        "取消回答",
        "打断一下",
        "不对",
        "不是",
        "我问的是",
        "换一个",
        "滚开",
        "别吵",
        "不要吵",
        "重新说",
        "重来",
    }
)


def normalize_transcript(text: str) -> str:
    return _PUNCTUATION.sub("", text).strip().lower()


def text_similarity(left: str, right: str) -> float:
    left_chars = set(normalize_transcript(left))
    right_chars = set(normalize_transcript(right))
    if not left_chars or not right_chars:
        return 0.0
    return len(left_chars & right_chars) / max(1, min(len(left_chars), len(right_chars)))


class LayeredInterruptionPolicy:
    """Recognize only a bounded, final command utterance as an interrupt."""

    def __init__(self, config: InterruptionPolicyConfig) -> None:
        self._config = config

    async def evaluate(self, context: InterruptionContext) -> InterruptionResult:
        if not self._config.enabled:
            return InterruptionResult("ignore", "policy_disabled")
        normalized = normalize_transcript(context.transcript)
        if not normalized:
            return InterruptionResult("ignore", "empty_transcript")
        if normalized in _BACKCHANNELS:
            return InterruptionResult("backchannel", "backchannel_phrase", 0.95)
        if normalized in _INTERRUPT_COMMANDS:
            if not context.is_final:
                return InterruptionResult("defer", "await_final_command", 0.9)
            return InterruptionResult("interrupt", "explicit_interrupt_phrase", 1.0)
        if not context.is_final:
            return InterruptionResult("defer", "await_final", 0.5)
        if context.assistant_text and text_similarity(normalized, context.assistant_text) >= 0.8:
            return InterruptionResult("echo_or_noise", "similar_to_assistant_text", 0.8)
        return InterruptionResult("ignore", "no_command_match", 1.0)


InterruptSink = Callable[[InterruptionContext], Awaitable[None]]


class InterruptionCoordinator:
    """Run one policy evaluation at a time and preserve the newest transcript."""

    def __init__(self, policy: InterruptionPolicy, interrupt: InterruptSink) -> None:
        self._policy = policy
        self._interrupt = interrupt
        self._revision = 0
        self._latest: tuple[int, InterruptionContext] | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    def submit(self, context: InterruptionContext) -> None:
        if self._closed:
            return
        self._revision += 1
        self._latest = (self._revision, context)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="interruption-coordinator")

    async def wait_idle(self) -> None:
        while self._task is not None:
            task = self._task
            await asyncio.shield(task)
            if task is self._task and task.done():
                self._task = None

    async def close(self) -> None:
        self._closed = True
        self._latest = None
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        while self._latest is not None:
            revision, context = self._latest
            self._latest = None
            result = await self._policy.evaluate(context)
            if self._latest is not None and self._latest[0] > revision:
                continue
            if result.should_interrupt:
                await self._interrupt(context)


__all__ = [
    "InterruptionContext",
    "InterruptionCoordinator",
    "InterruptionPolicy",
    "InterruptionPolicyConfig",
    "InterruptionResult",
    "LayeredInterruptionPolicy",
    "normalize_transcript",
    "text_similarity",
]
