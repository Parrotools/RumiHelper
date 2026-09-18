import asyncio

import pytest

from app.database.db import Database
from app.services.qq.dispatcher import MessageDispatcher


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "queue.db"))
    await database.connect()
    yield database
    await database.close()


async def test_outbound_queue_delivers_a_scheduled_message(db):
    async def api_caller(action: str, **params) -> dict:
        assert action == "send_private_msg"
        assert params == {"user_id": 123, "message": "提醒内容"}
        return {"message_id": 9}

    dispatcher = MessageDispatcher(db, api_caller=api_caller)
    message_id = await dispatcher.enqueue_user("123", "提醒内容")

    assert await dispatcher.process_outbound_messages() == 1
    row = await db.fetchone("SELECT status, attempts, message_id FROM outbound_messages WHERE id = ?", (message_id,))
    assert row == {"status": "sent", "attempts": 1, "message_id": "9"}


async def test_outbound_queue_retries_a_failed_message(db):
    attempts = 0

    async def api_caller(action: str, **params) -> dict:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary failure")
        return {"message_id": 10}

    dispatcher = MessageDispatcher(db, api_caller=api_caller, retry_delay_seconds=0)
    message_id = await dispatcher.enqueue_user("123", "稍后成功", max_attempts=3)

    await dispatcher.process_outbound_messages(limit=1)
    assert (await db.fetchone("SELECT status, attempts FROM outbound_messages WHERE id = ?", (message_id,))) == {
        "status": "retry",
        "attempts": 1,
    }
    await dispatcher.process_outbound_messages(limit=1)
    assert (await db.fetchone("SELECT status, attempts FROM outbound_messages WHERE id = ?", (message_id,))) == {
        "status": "sent",
        "attempts": 2,
    }


async def test_outbound_queue_marks_message_failed_after_max_attempts(db):
    async def api_caller(action: str, **params) -> dict:
        raise RuntimeError("permanent failure")

    dispatcher = MessageDispatcher(db, api_caller=api_caller, retry_delay_seconds=0)
    message_id = await dispatcher.enqueue_user("123", "最终失败", max_attempts=2)

    await dispatcher.process_outbound_messages()
    await dispatcher.process_outbound_messages()

    assert (await db.fetchone("SELECT status, attempts, last_error FROM outbound_messages WHERE id = ?", (message_id,))) == {
        "status": "failed",
        "attempts": 2,
        "last_error": "RuntimeError: permanent failure",
    }


async def test_outbound_queue_reclaims_expired_sending_lease(db):
    async def api_caller(action: str, **params) -> dict:
        return {"message_id": 11}

    dispatcher = MessageDispatcher(db, api_caller=api_caller)
    message_id = await dispatcher.enqueue_user("123", "恢复发送")
    claimed = await db.claim_outbound_message("9999-01-01 00:00:00", "2000-01-01 00:00:00")
    assert claimed["id"] == message_id
    await db.execute("UPDATE outbound_messages SET locked_at = ? WHERE id = ?", ("2000-01-01 00:00:00", message_id))

    assert await dispatcher.process_outbound_messages() == 1
    assert (await db.fetchone("SELECT status, attempts FROM outbound_messages WHERE id = ?", (message_id,))) == {
        "status": "sent",
        "attempts": 2,
    }


async def test_outbound_queue_pauses_after_account_offline(db):
    calls = 0

    async def api_caller(action: str, **params) -> dict:
        nonlocal calls
        calls += 1
        raise RuntimeError("KickedOffLine: login invalid")

    dispatcher = MessageDispatcher(
        db,
        api_caller=api_caller,
        retry_delay_seconds=0,
        account_pause_seconds=300,
    )
    message_id = await dispatcher.enqueue_group("123", "不要继续撞接口")

    assert await dispatcher.process_outbound_messages(limit=1) == 1
    assert await dispatcher.process_outbound_messages(limit=1) == 0
    assert calls == 1
    assert (await db.fetchone("SELECT status, attempts FROM outbound_messages WHERE id = ?", (message_id,))) == {
        "status": "retry",
        "attempts": 1,
    }


async def test_account_pause_is_persisted_across_dispatcher_restart(db):
    first_calls = 0

    async def offline_api(action: str, **params) -> dict:
        nonlocal first_calls
        first_calls += 1
        raise RuntimeError("KickedOffLine: login invalid")

    dispatcher = MessageDispatcher(
        db,
        api_caller=offline_api,
        retry_delay_seconds=0,
        account_pause_seconds=300,
    )
    first_id = await dispatcher.enqueue_group("123", "第一条")
    second_id = await dispatcher.enqueue_group("456", "第二条")

    assert await dispatcher.process_outbound_messages(limit=1) == 1
    third_id = await dispatcher.enqueue_group("789", "熔断期间新入队")
    assert first_calls == 1

    restarted_calls = 0

    async def should_not_run(action: str, **params) -> dict:
        nonlocal restarted_calls
        restarted_calls += 1
        return {"message_id": 99}

    restarted = MessageDispatcher(
        db,
        api_caller=should_not_run,
        retry_delay_seconds=0,
    )
    # A message created only after the process restart must still respect the
    # persisted global cooldown.
    fourth_id = await restarted.enqueue_group("999", "重启后新入队")

    assert await restarted.process_outbound_messages(limit=20) == 0
    assert restarted_calls == 0

    rows = await db.fetchall(
        "SELECT id, status, next_attempt_at FROM outbound_messages "
        "WHERE id IN (?, ?, ?, ?) ORDER BY id",
        (first_id, second_id, third_id, fourth_id),
    )
    assert [row["status"] for row in rows] == ["retry", "pending", "pending", "pending"]
    assert await db.fetch_outbound_pause_until() > "1970-01-01 00:00:00"


async def test_stop_finishes_inflight_delivery_before_worker_exits(db):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def api_caller(action: str, **params) -> dict:
        entered.set()
        await release.wait()
        return {"message_id": 321}

    dispatcher = MessageDispatcher(
        db,
        api_caller=api_caller,
        queue_poll_seconds=0.1,
    )
    await dispatcher.start()
    message_id = await dispatcher.enqueue_user("123", "graceful")

    await entered.wait()
    stopping = asyncio.create_task(dispatcher.stop())
    await asyncio.sleep(0)
    assert stopping.done() is False

    release.set()
    await stopping

    assert await db.fetchone(
        "SELECT status, message_id FROM outbound_messages WHERE id = ?",
        (message_id,),
    ) == {"status": "sent", "message_id": "321"}
    assert dispatcher._queue_task is None
