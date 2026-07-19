"""Select the configured lab TTS backend without changing AgentSession behavior."""

from __future__ import annotations

import asyncio

from livekit.agents import tts

from ..config import Settings
from ..observability.events import Tracer
from .cosyvoice_tts import CosyVoiceTTS
from .mimo_tts import MimoTTS
from .remote_cosyvoice_tts import RemoteCosyVoiceTTS

_WORKER_SEMAPHORES: dict[tuple[str, int], asyncio.Semaphore] = {}


def _worker_semaphore(provider: str, maximum: int) -> asyncio.Semaphore:
    key = (provider, maximum)
    semaphore = _WORKER_SEMAPHORES.get(key)
    if semaphore is None:
        semaphore = asyncio.Semaphore(maximum)
        _WORKER_SEMAPHORES[key] = semaphore
    return semaphore


async def create_tts(settings: Settings, *, tracer: Tracer | None = None) -> tts.TTS:
    if settings.tts_provider == "mimo":
        return await MimoTTS.create(
            settings,
            tracer=tracer,
            semaphore=_worker_semaphore("mimo", settings.mimo_max_concurrency),
        )
    if settings.tts_provider == "remote_cosyvoice":
        return await RemoteCosyVoiceTTS.create(
            settings,
            tracer=tracer,
            semaphore=_worker_semaphore("remote_cosyvoice", settings.remote_cosyvoice_max_concurrency),
        )
    return await CosyVoiceTTS.create(
        settings,
        tracer=tracer,
        semaphore=_worker_semaphore("cosyvoice", settings.cosyvoice_max_concurrency),
    )
