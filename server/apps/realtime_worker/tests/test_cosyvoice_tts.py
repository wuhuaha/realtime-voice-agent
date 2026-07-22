from __future__ import annotations

import asyncio

import httpx
import pytest
from realtime_worker.errors import BackpressureError
from realtime_worker.providers.cosyvoice_tts import CosyVoiceClient


def _pcm_response() -> httpx.Response:
    return httpx.Response(
        200,
        headers={"X-Audio-Format": "s16le", "X-Audio-Sample-Rate": "24000"},
        content=b"\x00\x00" * 480,
    )


@pytest.mark.asyncio
async def test_cosyvoice_concurrency_wait_times_out_and_permit_remains_available() -> None:
    semaphore = asyncio.Semaphore(1)
    await semaphore.acquire()
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return _pcm_response()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = CosyVoiceClient(
            "http://tts.test",
            client=http_client,
            semaphore=semaphore,
            queue_timeout_seconds=0.01,
        )
        with pytest.raises(BackpressureError) as error:
            _ = [frame async for frame in client.stream_pcm("blocked", mode="zero-shot", speaker="test")]

        assert error.value.retryable is True
        assert str(error.value) == "cosyvoice: TTS concurrency queue is full"
        assert requests == 0

        semaphore.release()
        frames = [frame async for frame in client.stream_pcm("next", mode="zero-shot", speaker="test")]

    assert frames
    assert requests == 1


@pytest.mark.asyncio
async def test_cosyvoice_cancelled_waiter_does_not_release_held_permit() -> None:
    semaphore = asyncio.Semaphore(1)
    await semaphore.acquire()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: _pcm_response())) as http_client:
        client = CosyVoiceClient(
            "http://tts.test",
            client=http_client,
            semaphore=semaphore,
            queue_timeout_seconds=1,
        )
        stream = client.stream_pcm("cancelled", mode="zero-shot", speaker="test")
        waiter = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        waiter.cancel()

        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert semaphore.locked()

        semaphore.release()
        frames = [frame async for frame in client.stream_pcm("next", mode="zero-shot", speaker="test")]

    assert frames
