"""Small bounded async queue with explicit close and drain semantics."""

from __future__ import annotations

import asyncio
from collections import deque


class AudioQueueClosed(RuntimeError):
    pass


class BoundedAudioQueue[T]:
    """A cancellation-safe queue whose close wakes blocked producers and consumers."""

    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._capacity = capacity
        self._items: deque[T] = deque()
        self._condition = asyncio.Condition()
        self._closed = False
        self._unfinished = 0

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def size(self) -> int:
        return len(self._items)

    @property
    def closed(self) -> bool:
        return self._closed

    async def put(self, item: T) -> None:
        async with self._condition:
            await self._condition.wait_for(lambda: self._closed or len(self._items) < self._capacity)
            if self._closed:
                raise AudioQueueClosed("audio queue is closed")
            self._items.append(item)
            self._unfinished += 1
            self._condition.notify_all()

    async def get(self) -> T:
        async with self._condition:
            await self._condition.wait_for(lambda: self._closed or bool(self._items))
            if not self._items:
                raise AudioQueueClosed("audio queue is closed")
            item = self._items.popleft()
            self._condition.notify_all()
            return item

    async def task_done(self) -> None:
        async with self._condition:
            if self._unfinished <= 0:
                raise ValueError("task_done called too many times")
            self._unfinished -= 1
            self._condition.notify_all()

    async def join(self) -> None:
        async with self._condition:
            await self._condition.wait_for(lambda: self._unfinished == 0)

    async def close(self, *, discard_pending: bool = False) -> None:
        async with self._condition:
            if discard_pending:
                self._unfinished -= len(self._items)
                self._items.clear()
            self._closed = True
            self._condition.notify_all()


__all__ = ["AudioQueueClosed", "BoundedAudioQueue"]
