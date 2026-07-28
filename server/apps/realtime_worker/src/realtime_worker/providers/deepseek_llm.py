"""DeepSeek configuration and Chinese speech-text preparation.

The LiveKit OpenAI plugin owns HTTP streaming.  This module makes the
DeepSeek-specific non-thinking contract explicit and keeps speech preparation
testable without requesting the provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from ..config import Settings
from ..errors import ProviderError


@dataclass(frozen=True)
class SpeechText:
    raw_text: str
    normalized_text: str


def create_deepseek_llm(settings: Settings) -> Any:
    """Create the locked OpenAI-compatible LLM without performing a network call."""

    settings.require_worker()
    from livekit.plugins import openai

    key = settings.deepseek_api_key
    assert key is not None  # validated above; retain secret only inside the plugin client
    return openai.LLM(
        model=settings.deepseek_model,
        base_url=settings.deepseek_base_url,
        api_key=key.get_secret_value(),
        # The plugin default is a 5 s read deadline.  That is too aggressive
        # for an Internet LLM stream: a provider can accept the request yet
        # legitimately take longer before its first token.  Keep connect and
        # pool bounds short, but give the streaming response a configured
        # per-read deadline so a brief provider pause does not discard a turn.
        timeout=httpx.Timeout(
            connect=15.0,
            read=settings.deepseek_read_timeout_seconds,
            write=10.0,
            pool=5.0,
        ),
        extra_body={"thinking": {"type": "disabled"}},
        temperature=0.7,
        max_completion_tokens=256,
    )


def visible_delta_content(delta: object) -> str:
    """Reject reasoning-only provider chunks before text reaches a speech queue."""

    reasoning = getattr(delta, "reasoning_content", None)
    if reasoning:
        raise ProviderError("deepseek", "reasoning content must not be sent to TTS", retryable=False)
    content = getattr(delta, "content", None)
    if content is None:
        return ""
    if not isinstance(content, str):
        raise ProviderError("deepseek", "provider returned non-text content", retryable=False)
    return content


class ChineseTTSChunker:
    """Conservative sentence/clause segmentation for streaming speech synthesis."""

    def __init__(self, *, min_clause_chars: int = 18) -> None:
        self._min_clause_chars = min_clause_chars
        self._buffer = ""

    def push(self, delta: str, *, provider_paused: bool = False) -> list[SpeechText]:
        self._buffer += delta
        chunks: list[SpeechText] = []
        while True:
            boundary = self._find_boundary(provider_paused=provider_paused)
            if boundary is None:
                return chunks
            raw = self._buffer[:boundary]
            self._buffer = self._buffer[boundary:]
            normalized = _normalize_speech_text(raw)
            if normalized:
                chunks.append(SpeechText(raw_text=raw, normalized_text=normalized))

    def flush(self) -> list[SpeechText]:
        raw = self._buffer
        self._buffer = ""
        normalized = _normalize_speech_text(raw)
        return [] if not normalized else [SpeechText(raw_text=raw, normalized_text=normalized)]

    def _find_boundary(self, *, provider_paused: bool) -> int | None:
        for index, char in enumerate(self._buffer):
            if char in "。！？":
                return index + 1
            if char in "，；：" and provider_paused and index + 1 >= self._min_clause_chars:
                return index + 1
        return None


def _normalize_speech_text(text: str) -> str:
    # The worker prompt forbids markup; this defensive pass avoids leaking partial
    # formatting into CosyVoice when a provider still emits it.
    from .cosyvoice_tts import normalize_for_tts

    return normalize_for_tts(text)
