from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_DETACHED_TASKS: set[asyncio.Task[object]] = set()
_DEFAULT_REAP_SECONDS = 0.05


@dataclass(frozen=True, slots=True)
class HardDeadlineResult[ResultT]:
    completed: bool
    value: ResultT | None = None


async def run_with_hard_deadline[ResultT](
    operation: Coroutine[object, object, ResultT],
    *,
    timeout: float,
    task_name: str,
    reap_timeout: float = _DEFAULT_REAP_SECONDS,
) -> HardDeadlineResult[ResultT]:
    """Bound an owned coroutine even when it ignores cancellation."""

    task = asyncio.create_task(operation, name=task_name)
    total_timeout = max(0.0, timeout)
    reap_budget = min(max(0.0, reap_timeout), total_timeout / 2)
    operation_budget = max(0.0, total_timeout - reap_budget)
    try:
        done, _ = await asyncio.wait({task}, timeout=operation_budget)
    except BaseException:
        # This helper owns the child it creates. Parent cancellation must not
        # leave that child running without a result consumer.
        await cancel_and_reap(task, task_name=task_name, reap_timeout=reap_budget)
        raise
    if done:
        return HardDeadlineResult(completed=True, value=task.result())

    await cancel_and_reap(task, task_name=task_name, reap_timeout=reap_budget)
    return HardDeadlineResult(completed=False)


async def cancel_and_reap[ResultT](
    task: asyncio.Task[ResultT],
    *,
    task_name: str,
    reap_timeout: float = _DEFAULT_REAP_SECONDS,
) -> bool:
    task.cancel()
    reaped, _ = await asyncio.wait({task}, timeout=max(0.0, reap_timeout))
    if reaped:
        if not task.cancelled():
            task.exception()
        return True

    retain_shutdown_task(task, task_name=task_name, cancel_requested=True)
    return False


def retain_shutdown_task[ResultT](
    task: asyncio.Task[ResultT],
    *,
    task_name: str,
    cancel_requested: bool,
) -> None:
    """Keep unfinished shutdown work strongly referenced and consume its result."""

    detached = task
    if detached in _DETACHED_TASKS:
        return
    _DETACHED_TASKS.add(detached)  # type: ignore[arg-type]
    logger.critical(
        "detached unfinished shutdown task task=%s cancel_requested=%s",
        task_name,
        cancel_requested,
    )

    def consume(completed: asyncio.Task[ResultT]) -> None:
        _DETACHED_TASKS.discard(completed)  # type: ignore[arg-type]
        if completed.cancelled():
            return
        exception = completed.exception()
        if exception is not None:
            logger.error(
                "detached shutdown task failed task=%s error_type=%s",
                task_name,
                type(exception).__name__,
            )

    detached.add_done_callback(consume)


def detached_shutdown_task_count() -> int:
    return len(_DETACHED_TASKS)


__all__ = [
    "HardDeadlineResult",
    "cancel_and_reap",
    "detached_shutdown_task_count",
    "retain_shutdown_task",
    "run_with_hard_deadline",
]
