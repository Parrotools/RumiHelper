"""Async coordination helpers for shared in-flight operations."""

from __future__ import annotations

import asyncio
from typing import TypeVar

T = TypeVar("T")


def _consume_done_exception(future: asyncio.Future[object]) -> None:
    """Mark a completed future's exception as retrieved without altering its result."""
    if not future.cancelled():
        future.exception()


async def await_shared_task(task: asyncio.Task[T]) -> T:
    """Await a shared task without propagating caller cancellation into it.

    Each caller gets a private waiter. Cancelling one waiter leaves the shared
    operation running for other callers. If every caller disappears and the
    shared task later fails, its exception is still retrieved.
    """
    loop = asyncio.get_running_loop()
    waiter: asyncio.Future[T] = loop.create_future()
    waiter.add_done_callback(_consume_done_exception)

    def complete(done: asyncio.Task[T]) -> None:
        if waiter.cancelled():
            _consume_done_exception(done)
            return
        if done.cancelled():
            waiter.cancel()
            return
        exception = done.exception()
        if exception is not None:
            waiter.set_exception(exception)
        else:
            waiter.set_result(done.result())

    task.add_done_callback(complete)
    return await waiter
