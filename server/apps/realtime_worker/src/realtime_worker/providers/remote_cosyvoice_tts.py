"""Remote CosyVoice3 adapter over an OpenAI-compatible PCM SSE endpoint."""

from __future__ import annotations

import asyncio

import httpx

from ..config import Settings
from ..observability.events import Tracer
from .mimo_tts import MimoTTS, MimoTTSClient


class RemoteCosyVoiceTTSClient(MimoTTSClient):
    def __init__(
        self,
        base_url: str,
        *,
        model: str,
        voice: str,
        timeout_seconds: float = 20.0,
        max_concurrency: int = 1,
        queue_timeout_seconds: float = 0.25,
        client: httpx.AsyncClient | None = None,
        semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        super().__init__(
            f"{base_url.rstrip('/')}/v1",
            "",
            model=model,
            provider="remote_cosyvoice",
            timeout_seconds=timeout_seconds,
            max_concurrency=max_concurrency,
            queue_timeout_seconds=queue_timeout_seconds,
            client=client,
            semaphore=semaphore,
        )
        self.voice = voice


class RemoteCosyVoiceTTS(MimoTTS):
    @classmethod
    async def create(
        cls,
        settings: Settings,
        *,
        tracer: Tracer | None = None,
        semaphore: asyncio.Semaphore | None = None,
    ) -> RemoteCosyVoiceTTS:
        settings.require_remote_cosyvoice_tts()
        return cls(
            RemoteCosyVoiceTTSClient(
                settings.remote_cosyvoice_url,
                model=settings.remote_cosyvoice_model,
                voice=settings.remote_cosyvoice_voice,
                timeout_seconds=settings.remote_cosyvoice_timeout_seconds,
                max_concurrency=settings.remote_cosyvoice_max_concurrency,
                queue_timeout_seconds=settings.tts_queue_timeout_seconds,
                semaphore=semaphore,
            ),
            settings=settings,
            tracer=tracer,
        )

    @property
    def model(self) -> str:
        return self._settings.remote_cosyvoice_model

    @property
    def provider(self) -> str:
        return "remote_cosyvoice"

    @property
    def voice(self) -> str:
        return self._settings.remote_cosyvoice_voice

    @property
    def style(self) -> str:
        return ""
