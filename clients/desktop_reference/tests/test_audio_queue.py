from __future__ import annotations

import asyncio

import pytest

from rva_desktop.audio import AudioQueueClosed, BoundedAudioQueue


def test_bounded_queue_applies_backpressure_and_join_tracks_consumption() -> None:
    async def scenario() -> None:
        queue: BoundedAudioQueue[int] = BoundedAudioQueue(1)
        await queue.put(1)
        blocked = asyncio.create_task(queue.put(2))
        await asyncio.sleep(0)
        assert not blocked.done()

        assert await queue.get() == 1
        await queue.task_done()
        await blocked
        assert await queue.get() == 2
        await queue.task_done()
        await queue.join()

    asyncio.run(scenario())


def test_close_wakes_blocked_producer_and_consumer() -> None:
    async def blocked_producer() -> None:
        queue: BoundedAudioQueue[int] = BoundedAudioQueue(1)
        await queue.put(1)
        producer = asyncio.create_task(queue.put(2))
        await asyncio.sleep(0)
        await queue.close(discard_pending=True)
        with pytest.raises(AudioQueueClosed):
            await producer
        await queue.join()

    async def blocked_consumer() -> None:
        queue: BoundedAudioQueue[int] = BoundedAudioQueue(1)
        consumer = asyncio.create_task(queue.get())
        await asyncio.sleep(0)
        await queue.close()
        with pytest.raises(AudioQueueClosed):
            await consumer

    asyncio.run(blocked_producer())
    asyncio.run(blocked_consumer())


def test_cancelled_put_does_not_enqueue_later() -> None:
    async def scenario() -> None:
        queue: BoundedAudioQueue[int] = BoundedAudioQueue(1)
        await queue.put(1)
        producer = asyncio.create_task(queue.put(2))
        await asyncio.sleep(0)
        producer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await producer
        assert await queue.get() == 1
        await queue.task_done()
        assert queue.size == 0

    asyncio.run(scenario())
