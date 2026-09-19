import asyncio

import pytest

from rumi_helper.services.async_utils import await_shared_task


async def test_cancelled_waiter_does_not_cancel_shared_task():
    release = asyncio.Event()

    async def operation():
        await release.wait()
        return "ok"

    shared = asyncio.create_task(operation())
    first = asyncio.create_task(await_shared_task(shared))
    second = asyncio.create_task(await_shared_task(shared))

    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    assert shared.cancelled() is False
    assert second.done() is False

    release.set()
    assert await second == "ok"
    assert await shared == "ok"


async def test_shared_exception_reaches_all_active_waiters():
    release = asyncio.Event()

    async def operation():
        await release.wait()
        raise RuntimeError("boom")

    shared = asyncio.create_task(operation())
    first = asyncio.create_task(await_shared_task(shared))
    second = asyncio.create_task(await_shared_task(shared))

    release.set()

    with pytest.raises(RuntimeError, match="boom"):
        await first
    with pytest.raises(RuntimeError, match="boom"):
        await second


async def test_shared_task_cancellation_propagates_to_waiter():
    shared = asyncio.create_task(asyncio.Event().wait())
    waiter = asyncio.create_task(await_shared_task(shared))

    await asyncio.sleep(0)
    shared.cancel()

    with pytest.raises(asyncio.CancelledError):
        await waiter
