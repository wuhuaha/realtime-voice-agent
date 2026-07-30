from __future__ import annotations

import asyncio

import pytest
from realtime_worker.lifecycle import detached_shutdown_task_count, retain_shutdown_task, run_with_hard_deadline


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


@pytest.mark.unit
async def test_retained_shutdown_task_is_not_cancelled_and_is_consumed_on_completion() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()
    cancelled = asyncio.Event()
    baseline = detached_shutdown_task_count()

    async def operation() -> None:
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        completed.set()

    task = asyncio.create_task(operation(), name="cancellation-unsafe-close")
    await started.wait()
    retain_shutdown_task(
        task,
        task_name="cancellation-unsafe-close",
        cancel_requested=False,
    )

    assert not cancelled.is_set()
    assert detached_shutdown_task_count() == baseline + 1

    release.set()
    for _ in range(20):
        if completed.is_set() and detached_shutdown_task_count() == baseline:
            break
        await asyncio.sleep(0)

    assert completed.is_set()
    assert detached_shutdown_task_count() == baseline
