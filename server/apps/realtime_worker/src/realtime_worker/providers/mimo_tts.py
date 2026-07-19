"""MiMo V2.5 streaming TTS adapter for the Chinese LiveKit lab."""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
from livekit.agents import DEFAULT_API_CONNECT_OPTIONS, tts

from ..config import Settings
from ..errors import ProviderError
from ..observability.events import Tracer, redact_exception
from .cosyvoice_tts import PCMFrame, _pcm_frames, normalize_for_tts

MIMO_SAMPLE_RATE = 24000
MIMO_AUDIO_FORMAT = "pcm16"


class MimoTTSClient:
    """A reusable HTTP/SSE client that never logs request bodies or credentials."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        model: str = "mimo-v2.5-tts",
        provider: str = "mimo",
        timeout_seconds: float = 20.0,
        max_concurrency: int = 1,
        client: httpx.AsyncClient | None = None,
        semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._provider = provider
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds))
        self._owns_client = client is None
        self._semaphore = semaphore or asyncio.Semaphore(max_concurrency)

    async def stream_pcm(
        self,
        text: str,
        *,
        voice: str,
        style: str,
        request_id: str | None = None,
    ) -> AsyncIterator[PCMFrame]:
        if not text.strip():
            return
        request_id = request_id or str(uuid.uuid4())
        messages: list[dict[str, str]] = []
        if style.strip():
            messages.append({"role": "user", "content": style.strip()})
        messages.append({"role": "assistant", "content": text})
        payload = {
            "model": self._model,
            "messages": messages,
            "audio": {"format": MIMO_AUDIO_FORMAT, "voice": voice},
            "stream": True,
        }
        headers = {"Accept": "text/event-stream"}
        if self._api_key:
            headers["api-key"] = self._api_key

        await self._semaphore.acquire()
        try:
            async with self._client.stream(
                "POST", f"{self._base_url}/chat/completions", headers=headers, json=payload
            ) as response:
                if response.status_code >= 400:
                    raise ProviderError(
                        self._provider,
                        f"TTS returned HTTP {response.status_code}",
                        retryable=response.status_code in {408, 429} or response.status_code >= 500,
                    )
                emitted_audio = False
                async for frame in _pcm_frames(
                    _mimo_pcm_chunks(response, provider=self._provider), MIMO_SAMPLE_RATE, request_id
                ):
                    emitted_audio = True
                    yield frame
                if not emitted_audio:
                    raise ProviderError(self._provider, "TTS returned no audio", retryable=True)
        except asyncio.CancelledError:
            raise
        except ProviderError:
            raise
        except httpx.HTTPError as exc:
            raise ProviderError(self._provider, "TTS connection failed", retryable=True) from exc
        finally:
            self._semaphore.release()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


async def _mimo_pcm_chunks(response: httpx.Response, *, provider: str = "mimo") -> AsyncIterator[bytes]:
    async for event in _sse_events(response):
        if event == "[DONE]":
            return
        try:
            payload = json.loads(event)
        except json.JSONDecodeError as exc:
            raise ProviderError(provider, "TTS returned invalid SSE JSON", retryable=False) from exc
        encoded_audio = _extract_audio_data(payload, provider=provider)
        if encoded_audio is None:
            continue
        try:
            audio = base64.b64decode(encoded_audio, validate=True)
        except (TypeError, ValueError) as exc:
            raise ProviderError(provider, "TTS returned invalid base64 audio", retryable=False) from exc
        if audio:
            yield audio


async def _sse_events(response: httpx.Response) -> AsyncIterator[str]:
    data_lines: list[str] = []
    async for raw_line in response.aiter_lines():
        line = raw_line.strip()
        if not line:
            if data_lines:
                yield "\n".join(data_lines)
                data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].strip())
    if data_lines:
        yield "\n".join(data_lines)


def _extract_audio_data(payload: object, *, provider: str = "mimo") -> str | None:
    if not isinstance(payload, dict):
        raise ProviderError(provider, "TTS SSE payload is not an object", retryable=False)
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        raise ProviderError(provider, "TTS SSE choice is invalid", retryable=False)
    delta = first_choice.get("delta")
    if not isinstance(delta, dict):
        return None
    audio = delta.get("audio")
    if audio is None:
        return None
    if not isinstance(audio, dict) or not isinstance(audio.get("data"), str):
        raise ProviderError(provider, "TTS SSE audio payload is invalid", retryable=False)
    return audio["data"]


class MimoTTS(tts.TTS):
    """LiveKit TTS implementation over MiMo's 24 kHz PCM SSE contract."""

    def __init__(self, client: MimoTTSClient, *, settings: Settings, tracer: Tracer | None = None) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=True, aligned_transcript=False),
            sample_rate=MIMO_SAMPLE_RATE,
            num_channels=1,
        )
        self._client = client
        self._settings = settings
        self._tracer = tracer

    @classmethod
    async def create(
        cls,
        settings: Settings,
        *,
        tracer: Tracer | None = None,
        semaphore: asyncio.Semaphore | None = None,
    ) -> MimoTTS:
        settings.require_mimo_tts()
        key = settings.mimo_api_key
        assert key is not None
        return cls(
            MimoTTSClient(
                settings.mimo_base_url,
                key.get_secret_value(),
                timeout_seconds=settings.mimo_timeout_seconds,
                max_concurrency=settings.mimo_max_concurrency,
                semaphore=semaphore,
            ),
            settings=settings,
            tracer=tracer,
        )

    @property
    def model(self) -> str:
        return self._settings.mimo_tts_model

    @property
    def provider(self) -> str:
        return "mimo"

    @property
    def voice(self) -> str:
        return self._settings.mimo_tts_voice

    @property
    def style(self) -> str:
        return self._settings.mimo_tts_style

    def synthesize(self, text: str, *, conn_options: Any = DEFAULT_API_CONNECT_OPTIONS) -> tts.ChunkedStream:
        return self._synthesize_with_stream(text, conn_options=conn_options)

    def stream(self, *, conn_options: Any = DEFAULT_API_CONNECT_OPTIONS) -> tts.SynthesizeStream:
        return _MimoSynthesizeStream(tts_instance=self, conn_options=conn_options)

    async def aclose(self) -> None:
        await self._client.aclose()


class _MimoSynthesizeStream(tts.SynthesizeStream):
    def __init__(self, *, tts_instance: MimoTTS, conn_options: Any) -> None:
        super().__init__(tts=tts_instance, conn_options=conn_options)
        self._mimo_tts = tts_instance

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        request_id = str(uuid.uuid4())
        output_emitter.initialize(
            request_id=request_id,
            sample_rate=self._mimo_tts.sample_rate,
            num_channels=1,
            mime_type="audio/pcm",
            frame_size_ms=20,
            stream=True,
        )
        pending = ""
        async for item in self._input_ch:
            if isinstance(item, self._FlushSentinel):
                if pending:
                    await self._synthesize_segment(pending, output_emitter, request_id)
                    pending = ""
                continue
            pending += item
        if pending:
            await self._synthesize_segment(pending, output_emitter, request_id)

    async def _synthesize_segment(self, raw_text: str, output_emitter: tts.AudioEmitter, request_id: str) -> None:
        text = normalize_for_tts(raw_text)
        if not text:
            return
        segment_id = str(uuid.uuid4())
        output_emitter.start_segment(segment_id=segment_id)
        tracer = self._mimo_tts._tracer
        provider = self._mimo_tts.provider
        turn_id = tracer.current_turn_id if tracer else None
        if tracer:
            tracer.event(
                "tts_requested",
                provider=provider,
                request_id=request_id,
                segment_id=segment_id,
                turn_id=turn_id,
                characters=len(text),
            )
        first_frame = True
        try:
            async for frame in self._mimo_tts._client.stream_pcm(
                text,
                voice=self._mimo_tts.voice,
                style=self._mimo_tts.style,
                request_id=request_id,
            ):
                if frame.sample_rate != self._mimo_tts.sample_rate:
                    raise ProviderError(provider, "sample rate changed during a stream", retryable=False)
                if first_frame and tracer:
                    tracer.event(
                        "tts_first_pcm",
                        provider=provider,
                        request_id=request_id,
                        segment_id=segment_id,
                        turn_id=turn_id,
                    )
                first_frame = False
                output_emitter.push(frame.data)
            if first_frame:
                raise ProviderError(provider, "TTS returned no audio", retryable=True)
            output_emitter.flush()
        except asyncio.CancelledError:
            raise
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(provider, redact_exception(exc), retryable=True) from exc
        finally:
            output_emitter.end_segment()
