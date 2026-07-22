from __future__ import annotations

import asyncio
import base64

import httpx
import pytest
from realtime_worker.config import Settings
from realtime_worker.errors import BackpressureError, ConfigurationError, ProviderError
from realtime_worker.providers import mimo_tts as mimo_module
from realtime_worker.providers import tts_factory
from realtime_worker.providers.mimo_tts import MimoTTSClient, _sse_events


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


class ChunkedByteStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        for chunk in self._chunks:
            yield chunk


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("limit_name", "content", "message"),
    [
        ("MIMO_MAX_SSE_LINE_BYTES", b"data: 12345678901234567", "SSE line is too large"),
        ("MIMO_MAX_SSE_EVENT_BYTES", b"data: 123456\ndata: abcdef\n\n", "SSE event is too large"),
        ("MIMO_MAX_RESPONSE_BYTES", b": keepalive\n: keepalive\n", "SSE response is too large"),
    ],
)
async def test_mimo_tts_rejects_oversized_sse_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
    content: bytes,
    message: str,
) -> None:
    monkeypatch.setattr(mimo_module, limit_name, 16 if limit_name != "MIMO_MAX_SSE_EVENT_BYTES" else 10)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=content))
    ) as http_client:
        client = MimoTTSClient("https://api.xiaomimimo.com/v1", "test-mimo-key", client=http_client)
        with pytest.raises(ProviderError, match=message) as error:
            _ = [frame async for frame in client.stream_pcm("bounded", voice="test", style="")]

    assert error.value.retryable is False


@pytest.mark.asyncio
async def test_mimo_tts_rejects_oversized_decoded_audio_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mimo_module, "MIMO_MAX_DECODED_AUDIO_CHUNK_BYTES", 2)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=_sse_audio(b"1234").encode()))
    ) as http_client:
        client = MimoTTSClient("https://api.xiaomimimo.com/v1", "test-mimo-key", client=http_client)
        with pytest.raises(ProviderError, match="audio chunk is too large") as error:
            _ = [frame async for frame in client.stream_pcm("bounded", voice="test", style="")]

    assert error.value.retryable is False


@pytest.mark.asyncio
async def test_sse_parser_supports_bom_and_cross_chunk_lf_crlf_and_cr_delimiters() -> None:
    response = httpx.Response(
        200,
        stream=ChunkedByteStream(
            [
                b"\xef",
                b"\xbb\xbfdata: one\r",
                b"\n\rdata: two\r",
                b"\rdata: three\n",
                b"\n",
            ]
        ),
    )
    try:
        assert [event async for event in _sse_events(response)] == ["one", "two", "three"]
    finally:
        await response.aclose()


@pytest.mark.asyncio
async def test_mimo_tts_concurrency_wait_times_out_and_permit_remains_available() -> None:
    semaphore = asyncio.Semaphore(1)
    await semaphore.acquire()
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, content=_sse_audio(b"\x00\x00" * 480).encode())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = MimoTTSClient(
            "https://api.xiaomimimo.com/v1",
            "test-mimo-key",
            client=http_client,
            semaphore=semaphore,
            queue_timeout_seconds=0.01,
        )
        with pytest.raises(BackpressureError) as error:
            _ = [frame async for frame in client.stream_pcm("blocked", voice="test", style="")]

        assert error.value.retryable is True
        assert str(error.value) == "mimo: TTS concurrency queue is full"
        assert requests == 0

        semaphore.release()
        frames = [frame async for frame in client.stream_pcm("next", voice="test", style="")]

    assert frames
    assert requests == 1


@pytest.mark.asyncio
async def test_mimo_tts_cancelled_waiter_does_not_release_held_permit() -> None:
    semaphore = asyncio.Semaphore(1)
    await semaphore.acquire()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=_sse_audio(b"\x00\x00" * 480)))
    ) as http_client:
        client = MimoTTSClient(
            "https://api.xiaomimimo.com/v1",
            "test-mimo-key",
            client=http_client,
            semaphore=semaphore,
            queue_timeout_seconds=1,
        )
        stream = client.stream_pcm("cancelled", voice="test", style="")
        waiter = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        waiter.cancel()

        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert semaphore.locked()

        semaphore.release()
        frames = [frame async for frame in client.stream_pcm("next", voice="test", style="")]

    assert frames


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
