from __future__ import annotations

import base64
import json

import httpx
import pytest
from realtime_worker.config import Settings
from realtime_worker.errors import ConfigurationError, ProviderError
from realtime_worker.providers import tts_factory
from realtime_worker.providers.remote_cosyvoice_tts import RemoteCosyVoiceTTSClient


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "livekit_url": "ws://127.0.0.1:7880",
        "livekit_public_url": "ws://192.168.50.10:7880",
        "livekit_api_key": "test-key",
        "livekit_api_secret": "test-secret-with-at-least-thirty-two-bytes",
        "deepseek_api_key": "not-used-by-provider-tests",
        "remote_cosyvoice_url": "http://tts.test:2222",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _sse_audio(data: bytes) -> bytes:
    encoded = base64.b64encode(data).decode("ascii")
    return (f'data: {{"choices":[{{"delta":{{"audio":{{"data":"{encoded}"}}}}}}]}}\n\ndata: [DONE]\n\n').encode()


@pytest.mark.asyncio
async def test_remote_cosyvoice_sends_named_model_voice_and_streams_pcm() -> None:
    pcm = b"\x01\x00" * 600

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "http://tts.test:2222/v1/chat/completions"
        assert "api-key" not in request.headers
        body = json.loads(request.read())
        assert body == {
            "model": "cosyvoice3",
            "messages": [{"role": "assistant", "content": "你好"}],
            "audio": {"format": "pcm16", "voice": "mumu"},
            "stream": True,
        }
        return httpx.Response(200, content=_sse_audio(pcm))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = RemoteCosyVoiceTTSClient(
            "http://tts.test:2222",
            model="cosyvoice3",
            voice="mumu",
            client=http_client,
        )
        frames = [frame async for frame in client.stream_pcm("你好", voice="mumu", style="", request_id="r1")]

    assert b"".join(frame.data for frame in frames) == pcm
    assert all(frame.sample_rate == 24000 for frame in frames)


@pytest.mark.asyncio
async def test_remote_cosyvoice_does_not_expose_provider_error_body() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(503, text="private-server-detail"))
    ) as http_client:
        client = RemoteCosyVoiceTTSClient(
            "http://tts.test:2222",
            model="cosyvoice3",
            voice="mumu",
            client=http_client,
        )
        with pytest.raises(ProviderError) as error:
            _ = [frame async for frame in client.stream_pcm("你好", voice="mumu", style="")]

    assert "HTTP 503" in str(error.value)
    assert "private-server-detail" not in str(error.value)
    assert error.value.retryable is True


def test_remote_cosyvoice_requires_an_absolute_url_when_selected() -> None:
    settings = make_settings(tts_provider="remote_cosyvoice", remote_cosyvoice_url="")

    with pytest.raises(ConfigurationError, match="REMOTE_COSYVOICE_URL"):
        settings.require_worker()


@pytest.mark.asyncio
async def test_tts_factory_selects_remote_cosyvoice(monkeypatch: pytest.MonkeyPatch) -> None:
    selected = object()

    async def create_remote(
        _settings: Settings,
        *,
        tracer: object | None = None,
        semaphore: object | None = None,
    ) -> object:
        assert tracer is None
        assert semaphore is not None
        return selected

    monkeypatch.setattr(tts_factory.RemoteCosyVoiceTTS, "create", create_remote)

    result = await tts_factory.create_tts(make_settings(tts_provider="remote_cosyvoice"))

    assert result is selected
