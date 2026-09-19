import asyncio

import pytest

from rumi_helper.database.db import Database
from rumi_helper.services.qq.dispatcher import MessageDispatcher
from rumi_helper.utils import QQ_TEXT_SAFE_LIMIT


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
    row = await db.fetchone(
        "SELECT status, attempts, message_id, content FROM outbound_messages WHERE id = ?",
        (message_id,),
    )
    assert row == {"status": "sent", "attempts": 1, "message_id": "9", "content": ""}


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

    assert (
        await db.fetchone(
            "SELECT status, attempts, last_error, content FROM outbound_messages WHERE id = ?",
            (message_id,),
        )
    ) == {
        "status": "failed",
        "attempts": 2,
        "last_error": "RuntimeError",
        "content": "",
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
    assert dispatcher.worker_running is False
    await dispatcher.start()
    assert dispatcher.worker_running is True
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
    assert dispatcher.worker_running is False


async def test_stop_is_bounded_when_api_swallows_first_cancel(db):
    entered = asyncio.Event()
    swallowed = asyncio.Event()

    async def api_caller(action: str, **params) -> dict:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            swallowed.set()
            await asyncio.Event().wait()
        return {"message_id": 999}

    dispatcher = MessageDispatcher(
        db,
        api_caller=api_caller,
        queue_poll_seconds=0.1,
        shutdown_timeout_seconds=0.1,
    )
    await dispatcher.start()
    await dispatcher.enqueue_user("123", "stubborn")
    worker = dispatcher._queue_task
    assert worker is not None
    await entered.wait()

    await asyncio.wait_for(dispatcher.stop(), timeout=0.5)
    assert swallowed.is_set()
    assert dispatcher._queue_task is None

    with pytest.raises(asyncio.CancelledError):
        await worker


async def test_malformed_onebot_response_retries_instead_of_marking_sent(db):
    async def api_caller(action: str, **params):
        return {}

    dispatcher = MessageDispatcher(
        db,
        api_caller=api_caller,
        retry_delay_seconds=0,
    )
    message_id = await dispatcher.enqueue_user(
        "123",
        "must not be marked sent",
        max_attempts=2,
    )

    assert await dispatcher.process_outbound_messages(limit=1) == 1
    row = await db.fetchone(
        "SELECT status, attempts, last_error, message_id "
        "FROM outbound_messages WHERE id = ?",
        (message_id,),
    )
    assert row["status"] == "retry"
    assert row["attempts"] == 1
    assert row["message_id"] is None
    assert row["last_error"] == "ValueError"


async def test_enqueue_truncates_oversized_message_before_persistence(db):
    dispatcher = MessageDispatcher(db)
    message_id = await dispatcher.enqueue_user(
        "123",
        "x" * (QQ_TEXT_SAFE_LIMIT + 500),
    )

    row = await db.fetchone(
        "SELECT content FROM outbound_messages WHERE id = ?",
        (message_id,),
    )
    assert row is not None
    assert len(row["content"]) <= QQ_TEXT_SAFE_LIMIT
    assert "内容过长已截断" in row["content"]

async def test_repository_outbox_apis_bound_content_without_dispatcher(db):
    oversized = "x" * (QQ_TEXT_SAFE_LIMIT + 500)
    first_id = await db.enqueue_outbound_message(
        "user",
        "123",
        oversized,
        3,
    )
    assert await db.enqueue_outbound_messages(
        [
            {
                "target_type": "group",
                "target_id": "456",
                "content": oversized,
            }
        ],
        3,
    ) == 1

    rows = await db.fetchall(
        "SELECT id, content FROM outbound_messages ORDER BY id",
        (),
    )
    assert rows[0]["id"] == first_id
    assert len(rows) == 2
    for row in rows:
        assert len(row["content"]) <= QQ_TEXT_SAFE_LIMIT
        assert "内容过长已截断" in row["content"]


async def test_zero_history_limit_deletes_terminal_rows_but_preserves_live_queue(db):
    sent_id = await db.enqueue_outbound_message("user", "101", "sent", 3)
    failed_id = await db.enqueue_outbound_message("user", "102", "failed", 1)
    pending_id = await db.enqueue_outbound_message("user", "103", "pending", 3)
    await db.mark_outbound_message_sent(sent_id, "2030-01-01 00:00:00", "m1")
    await db.retry_outbound_message(
        failed_id,
        1,
        1,
        "2030-01-01 00:00:00",
        "SendError",
    )
    await db.insert_send_log("user", "101", "body", True, "m1", None)

    await db.prune_terminal_outbound_messages(0)
    await db.prune_send_logs(0)

    assert await db.fetchall(
        "SELECT id, status FROM outbound_messages ORDER BY id",
        (),
    ) == [{"id": pending_id, "status": "pending"}]
    assert await db.fetchone(
        "SELECT COUNT(*) AS count FROM send_logs",
        (),
    ) == {"count": 0}


async def test_delivery_history_pruning_preserves_active_queue_rows(db):
    sent_id = await db.enqueue_outbound_message("user", "101", "sent-old", 3)
    failed_id = await db.enqueue_outbound_message("user", "102", "failed-old", 1)
    recent_sent_id = await db.enqueue_outbound_message("user", "103", "sent-new", 3)
    pending_id = await db.enqueue_outbound_message("user", "104", "pending", 3)

    await db.mark_outbound_message_sent(
        sent_id,
        "2030-01-01 00:00:00",
        "m1",
    )
    await db.retry_outbound_message(
        failed_id,
        1,
        1,
        "2030-01-01 00:00:00",
        "SendError",
    )
    await db.mark_outbound_message_sent(
        recent_sent_id,
        "2030-01-01 00:00:01",
        "m2",
    )

    await db.prune_terminal_outbound_messages(2)

    rows = await db.fetchall(
        "SELECT id, status FROM outbound_messages ORDER BY id",
        (),
    )
    assert rows == [
        {"id": failed_id, "status": "failed"},
        {"id": recent_sent_id, "status": "sent"},
        {"id": pending_id, "status": "pending"},
    ]

    for index in range(4):
        await db.insert_send_log(
            "user",
            str(index),
            "body",
            index % 2 == 0,
            str(index),
            None,
        )
    await db.prune_send_logs(2)
    assert await db.fetchall(
        "SELECT target_id FROM send_logs ORDER BY id",
        (),
    ) == [{"target_id": "2"}, {"target_id": "3"}]


async def test_dispatcher_start_prunes_existing_delivery_history(db):
    for index in range(3):
        message_id = await db.enqueue_outbound_message(
            "user",
            str(100 + index),
            f"sent-{index}",
            3,
        )
        await db.mark_outbound_message_sent(
            message_id,
            f"2030-01-01 00:00:0{index}",
            f"m{index}",
        )
        await db.insert_send_log(
            "user",
            str(index),
            "body",
            True,
            f"m{index}",
            None,
        )

    dispatcher = MessageDispatcher(
        db,
        terminal_history_limit=1,
        send_log_history_limit=1,
        queue_poll_seconds=60,
    )
    await dispatcher.start()
    await dispatcher.stop()

    assert await db.fetchone(
        "SELECT COUNT(*) AS count FROM outbound_messages "
        "WHERE status IN ('sent', 'failed')",
        (),
    ) == {"count": 1}
    assert await db.fetchone(
        "SELECT COUNT(*) AS count FROM send_logs",
        (),
    ) == {"count": 1}
