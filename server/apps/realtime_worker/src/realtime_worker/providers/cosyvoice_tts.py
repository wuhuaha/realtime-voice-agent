"""CosyVoice gateway client and LiveKit TTS adapter.

The gateway is the sampling-rate authority.  This adapter never silently
resamples audio: a future resampler must be inserted as its own measured
component rather than being hidden in the HTTP client.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
from livekit import rtc
from livekit.agents import DEFAULT_API_CONNECT_OPTIONS, tts

from ..config import Settings
from ..errors import BackpressureError, ProviderError
from ..observability.events import Tracer, redact_exception


@dataclass(frozen=True)
class GatewayHealth:
    model_id: str
    sample_rate: int
    audio_format: str
    device: str
    speakers: tuple[str, ...]


@dataclass(frozen=True)
class PCMFrame:
    data: bytes
    sample_rate: int
    samples_per_channel: int
    request_id: str

    def to_rtc(self) -> rtc.AudioFrame:
        return rtc.AudioFrame(
            data=self.data,
            sample_rate=self.sample_rate,
            num_channels=1,
            samples_per_channel=self.samples_per_channel,
        )


async def _acquire_tts_slot(
    semaphore: asyncio.Semaphore,
    *,
    timeout_seconds: float,
    provider: str,
) -> None:
    try:
        await asyncio.wait_for(semaphore.acquire(), timeout=timeout_seconds)
    except TimeoutError:
        raise BackpressureError(provider, "TTS concurrency") from None


class CosyVoiceClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 30.0,
        max_concurrency: int = 1,
        queue_timeout_seconds: float = 0.25,
        client: httpx.AsyncClient | None = None,
        semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds))
        self._owns_client = client is None
        self._semaphore = semaphore or asyncio.Semaphore(max_concurrency)
        self._queue_timeout_seconds = queue_timeout_seconds

    async def health(self) -> GatewayHealth:
        try:
            response = await self._client.get(f"{self._base_url}/healthz")
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError("cosyvoice", "health check failed", retryable=True) from exc
        try:
            sample_rate = int(body["sample_rate"])
            if sample_rate <= 0 or body["audio_format"] != "s16le":
                raise ValueError("unsupported audio metadata")
            speakers = tuple(str(value) for value in body.get("speakers", []))
            return GatewayHealth(
                model_id=str(body["model_id"]),
                sample_rate=sample_rate,
                audio_format="s16le",
                device=str(body["device"]),
                speakers=speakers,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError("cosyvoice", "invalid health response", retryable=False) from exc

    async def stream_pcm(
        self,
        text: str,
        *,
        mode: str,
        speaker: str,
        request_id: str | None = None,
    ) -> AsyncIterator[PCMFrame]:
        if not text.strip():
            return
        request_id = request_id or str(uuid.uuid4())
        payload = {
            "request_id": request_id,
            "text": text,
            "mode": mode,
            "speaker": speaker,
            "speed": 1.0,
        }
        await _acquire_tts_slot(
            self._semaphore,
            timeout_seconds=self._queue_timeout_seconds,
            provider="cosyvoice",
        )
        try:
            async with self._client.stream("POST", f"{self._base_url}/v1/tts", json=payload) as response:
                if response.status_code >= 400:
                    raise ProviderError(
                        "cosyvoice",
                        f"gateway returned HTTP {response.status_code}",
                        retryable=response.status_code >= 500,
                    )
                sample_rate = _read_sample_rate(response)
                actual_request_id = response.headers.get("X-Request-Id", request_id)
                async for frame in _pcm_frames(response.aiter_bytes(), sample_rate, actual_request_id):
                    yield frame
        except asyncio.CancelledError:
            raise
        except ProviderError:
            raise
        except httpx.HTTPError as exc:
            raise ProviderError("cosyvoice", "gateway connection failed", retryable=True) from exc
        finally:
            self._semaphore.release()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _read_sample_rate(response: httpx.Response) -> int:
    if response.headers.get("X-Audio-Format") != "s16le":
        raise ProviderError("cosyvoice", "gateway did not return s16le PCM", retryable=False)
    try:
        sample_rate = int(response.headers["X-Audio-Sample-Rate"])
    except (KeyError, ValueError) as exc:
        raise ProviderError("cosyvoice", "gateway omitted a valid sample rate", retryable=False) from exc
    if sample_rate <= 0:
        raise ProviderError("cosyvoice", "gateway returned a non-positive sample rate", retryable=False)
    return sample_rate


async def _pcm_frames(
    chunks: AsyncIterator[bytes], sample_rate: int, request_id: str, frame_ms: int = 20
) -> AsyncIterator[PCMFrame]:
    samples = sample_rate * frame_ms // 1000
    if samples <= 0:
        raise ProviderError("cosyvoice", "sample rate cannot form a 20 ms frame", retryable=False)
    frame_bytes = samples * 2
    remainder = b""
    async for chunk in chunks:
        data = remainder + chunk
        usable = len(data) - (len(data) % frame_bytes)
        for start in range(0, usable, frame_bytes):
            frame = data[start : start + frame_bytes]
            yield PCMFrame(frame, sample_rate, len(frame) // 2, request_id)
        remainder = data[usable:]
    if remainder:
        if len(remainder) % 2:
            raise ProviderError("cosyvoice", "gateway returned an odd PCM byte count", retryable=False)
        yield PCMFrame(remainder, sample_rate, len(remainder) // 2, request_id)


def normalize_for_tts(text: str) -> str:
    """Remove formatting/control characters without changing ordinary Chinese prose."""

    without_blocks = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    without_markdown = re.sub(r"[*_`#>|~]", "", without_blocks)
    printable = "".join(char for char in without_markdown if char.isprintable() or char.isspace())
    return re.sub(r"\s+", " ", printable).strip()


class CosyVoiceTTS(tts.TTS):
    def __init__(
        self,
        client: CosyVoiceClient,
        *,
        health: GatewayHealth,
        settings: Settings,
        tracer: Tracer | None = None,
    ) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=True, aligned_transcript=False),
            sample_rate=health.sample_rate,
            num_channels=1,
        )
        self._client = client
        self._health = health
        self._settings = settings
        self._tracer = tracer

    @classmethod
    async def create(
        cls,
        settings: Settings,
        *,
        tracer: Tracer | None = None,
        semaphore: asyncio.Semaphore | None = None,
    ) -> CosyVoiceTTS:
        client = CosyVoiceClient(
            settings.cosyvoice_url,
            timeout_seconds=settings.cosyvoice_timeout_seconds,
            max_concurrency=settings.cosyvoice_max_concurrency,
            queue_timeout_seconds=settings.tts_queue_timeout_seconds,
            semaphore=semaphore,
        )
        try:
            health = await client.health()
        except Exception:
            await client.aclose()
            raise
        return cls(client, health=health, settings=settings, tracer=tracer)

    @property
    def model(self) -> str:
        return self._health.model_id

    @property
    def provider(self) -> str:
        return "cosyvoice"

    def synthesize(self, text: str, *, conn_options: Any = DEFAULT_API_CONNECT_OPTIONS) -> tts.ChunkedStream:
        return self._synthesize_with_stream(text, conn_options=conn_options)

    def stream(self, *, conn_options: Any = DEFAULT_API_CONNECT_OPTIONS) -> tts.SynthesizeStream:
        return _CosyVoiceSynthesizeStream(tts_instance=self, conn_options=conn_options)

    async def aclose(self) -> None:
        await self._client.aclose()


class _CosyVoiceSynthesizeStream(tts.SynthesizeStream):
    def __init__(self, *, tts_instance: CosyVoiceTTS, conn_options: Any) -> None:
        super().__init__(tts=tts_instance, conn_options=conn_options)
        self._cosy_tts = tts_instance

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        request_id = str(uuid.uuid4())
        output_emitter.initialize(
            request_id=request_id,
            sample_rate=self._cosy_tts.sample_rate,
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
        tracer = self._cosy_tts._tracer
        if tracer:
            tracer.event("tts_requested", provider="cosyvoice", request_id=request_id, characters=len(text))
        first_frame = True
        try:
            async for frame in self._cosy_tts._client.stream_pcm(
                text,
                mode=self._cosy_tts._settings.cosyvoice_mode,
                speaker=self._cosy_tts._settings.cosyvoice_speaker,
                request_id=request_id,
            ):
                if frame.sample_rate != self._cosy_tts.sample_rate:
                    raise ProviderError("cosyvoice", "sample rate changed during a stream", retryable=False)
                if first_frame and tracer:
                    tracer.event("tts_first_pcm", provider="cosyvoice", request_id=request_id)
                first_frame = False
                output_emitter.push(frame.data)
            if first_frame:
                raise ProviderError("cosyvoice", "gateway returned no audio", retryable=True)
            output_emitter.flush()
        except asyncio.CancelledError:
            raise
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("cosyvoice", redact_exception(exc), retryable=True) from exc
        finally:
            output_emitter.end_segment()
