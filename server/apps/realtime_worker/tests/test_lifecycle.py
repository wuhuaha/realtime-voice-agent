from __future__ import annotations

import asyncio

import pytest
from realtime_worker.lifecycle import detached_shutdown_task_count, run_with_hard_deadline


@pytest.mark.unit
async def test_hard_deadline_reaps_owned_child_when_parent_is_cancelled() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()
    baseline = detached_shutdown_task_count()

    async def operation() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    parent = asyncio.create_task(
        run_with_hard_deadline(operation(), timeout=1.0, task_name="cancelled-owned-child"),
    )
    await started.wait()
    parent.cancel()

    with pytest.raises(asyncio.CancelledError):
        await parent

    assert cancelled.is_set()
    assert detached_shutdown_task_count() == baseline
    assert not any(task.get_name() == "cancelled-owned-child" for task in asyncio.all_tasks())
