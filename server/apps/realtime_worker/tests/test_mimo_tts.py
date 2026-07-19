from __future__ import annotations

import base64

import httpx
import pytest
from realtime_worker.config import Settings
from realtime_worker.errors import ConfigurationError, ProviderError
from realtime_worker.providers import tts_factory
from realtime_worker.providers.mimo_tts import MimoTTSClient


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "livekit_url": "ws://127.0.0.1:7880",
        "livekit_public_url": "ws://192.168.50.10:7880",
        "livekit_api_key": "test-key",
        "livekit_api_secret": "test-secret-with-at-least-thirty-two-bytes",
        "deepseek_api_key": "not-used-by-token-tests",
        "cosyvoice_model": "local-model-path",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _sse_audio(data: bytes) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return f'data: {{"choices":[{{"delta":{{"audio":{{"data":"{encoded}"}}}}}}]}}\n\n'


@pytest.mark.asyncio
async def test_mimo_tts_sse_audio_is_framed_as_pcm16() -> None:
    first = b"\x00\x00" * 600
    second = b"\x01\x00" * 400

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.xiaomimimo.com/v1/chat/completions"
        assert request.headers["api-key"] == "test-mimo-key"
        body = request.read().decode("utf-8")
        assert '"model":"mimo-v2.5-tts"' in body
        assert '"format":"pcm16"' in body
        return httpx.Response(200, content=(_sse_audio(first) + _sse_audio(second) + "data: [DONE]\n\n").encode())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = MimoTTSClient("https://api.xiaomimimo.com/v1", "test-mimo-key", client=http_client)
        frames = [frame async for frame in client.stream_pcm("你好", voice="冰糖", style="", request_id="r1")]

    assert b"".join(frame.data for frame in frames) == first + second
    assert [len(frame.data) for frame in frames] == [960, 960, 80]
    assert [frame.samples_per_channel for frame in frames] == [480, 480, 40]
    assert all(frame.sample_rate == 24000 for frame in frames)


@pytest.mark.asyncio
async def test_mimo_tts_never_exposes_provider_error_body() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(401, text="private-provider-detail"))
    ) as http_client:
        client = MimoTTSClient("https://api.xiaomimimo.com/v1", "test-mimo-key", client=http_client)
        with pytest.raises(ProviderError) as error:
            _ = [frame async for frame in client.stream_pcm("你好", voice="冰糖", style="")]

    assert "HTTP 401" in str(error.value)
    assert "private-provider-detail" not in str(error.value)
    assert error.value.retryable is False


def test_mimo_tts_requires_a_key_when_selected() -> None:
    settings = make_settings(tts_provider="mimo", mimo_api_key=None)

    with pytest.raises(ConfigurationError, match="MIMO_API_KEY"):
        settings.require_worker()


@pytest.mark.asyncio
async def test_tts_factory_selects_mimo_without_constructing_cosyvoice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = object()

    async def create_mimo(
        _settings: Settings,
        *,
        tracer: object | None = None,
        semaphore: object | None = None,
    ) -> object:
        assert tracer is None
        assert semaphore is not None
        return selected

    monkeypatch.setattr(tts_factory.MimoTTS, "create", create_mimo)

    result = await tts_factory.create_tts(make_settings(tts_provider="mimo", mimo_api_key="test-mimo-key"))

    assert result is selected


def test_tts_factory_semaphore_is_shared_at_worker_scope() -> None:
    first = tts_factory._worker_semaphore("mimo", 1)  # noqa: SLF001
    second = tts_factory._worker_semaphore("mimo", 1)  # noqa: SLF001
    other = tts_factory._worker_semaphore("remote_cosyvoice", 1)  # noqa: SLF001
    assert first is second
    assert first is not other
