"""MessageDispatcher 测试：发送、失败记录、broadcast 部分失败继续、限速间隔。"""

import asyncio
import time

import pytest

from rumi_helper.database.db import Database
from rumi_helper.services.qq.broadcast_parser import BroadcastTarget
from rumi_helper.services.qq.dispatcher import MessageDispatcher
from rumi_helper.utils import QQ_TEXT_SAFE_LIMIT


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    await d.connect()
    yield d
    await d.close()


def _fake_caller(fail_targets: set[str]):
    async def caller(action: str, **params) -> dict:
        tid = str(params.get("user_id") or params.get("group_id"))
        if tid in fail_targets:
            raise RuntimeError("API error")
        return {"message_id": f"mid-{tid}"}

    return caller


@pytest.mark.parametrize(
    ("target_type", "target_id"),
    [
        ("channel", "123"),
        ("user", "-1"),
        ("group", "１２３"),
        ("user", "0"),
        ("group", "000"),
        ("user", ""),
    ],
)
async def test_send_rejects_invalid_target_before_onebot_call(
    db,
    target_type,
    target_id,
):
    calls = 0

    async def caller(action: str, **params) -> dict:
        nonlocal calls
        calls += 1
        return {"message_id": "unexpected"}

    dispatcher = MessageDispatcher(
        db,
        rate_limit_per_second=0,
        api_caller=caller,
    )

    result = await dispatcher._send(target_type, target_id, "hello")

    assert result.success is False
    assert result.error == "InvalidTarget"
    assert calls == 0
    logs = await db.fetchall(
        "SELECT target_type, target_id, success, error FROM send_logs",
        (),
    )
    assert logs == [
        {
            "target_type": target_type,
            "target_id": target_id,
            "success": 0,
            "error": "InvalidTarget",
        }
    ]


async def test_send_user_success_logs(db):
    d = MessageDispatcher(db, rate_limit_per_second=0, api_caller=_fake_caller(set()))
    r = await d.send_user("123", "hello")
    assert r.success and r.message_id == "mid-123"
    logs = await db.fetchall("SELECT * FROM send_logs")
    assert len(logs) == 1
    assert logs[0]["success"] == 1
    assert logs[0]["target_type"] == "user"
    assert logs[0]["content"] == ""


async def test_direct_send_forces_onebot_plain_text_mode(db):
    captured = {}

    async def caller(action: str, **params) -> dict:
        captured.update(action=action, **params)
        return {"message_id": "mid-1"}

    dispatcher = MessageDispatcher(
        db,
        rate_limit_per_second=0,
        api_caller=caller,
    )
    payload = "[CQ:at,qq=all] [CQ:image,file=https://example.com/x]"

    result = await dispatcher.send_group("456", payload)

    assert result.success is True
    assert captured["action"] == "send_group_msg"
    assert captured["group_id"] == 456
    assert captured["message"] == payload
    assert captured["auto_escape"] is True


async def test_send_failure_recorded(db):
    d = MessageDispatcher(db, rate_limit_per_second=0, api_caller=_fake_caller({"999"}))
    r = await d.send_group("999", "hi")
    assert not r.success
    assert r.error
    logs = await db.fetchall("SELECT * FROM send_logs WHERE success = 0")
    assert len(logs) == 1


async def test_broadcast_continues_on_failure(db):
    d = MessageDispatcher(db, rate_limit_per_second=0, api_caller=_fake_caller({"456"}))
    targets = [BroadcastTarget("user", "123"), BroadcastTarget("group", "456"), BroadcastTarget("user", "789")]
    report = await d.broadcast(targets, "msg")
    assert report.success_count == 2
    assert report.failed_count == 1
    assert report.failed_lines() == ["group:456"]
    text = report.summary_text()
    assert "成功：2" in text and "失败：1" in text
    assert len(await db.fetchall("SELECT * FROM send_logs")) == 3


async def test_broadcast_rate_limit(db):
    d = MessageDispatcher(db, rate_limit_per_second=50, api_caller=_fake_caller(set()))
    targets = [BroadcastTarget("user", str(i)) for i in range(1, 5)]
    start = time.monotonic()
    await d.broadcast(targets, "msg")
    elapsed = time.monotonic() - start
    # 3 次间隔 * 1/50s = 0.06s，至少要有间隔（保守断言避免抖动）
    assert elapsed >= 0.045


async def test_concurrent_sends_share_database_wide_rate_limit(db):
    started: list[float] = []

    async def caller(action: str, **params) -> dict:
        started.append(time.monotonic())
        return {"message_id": len(started)}

    dispatcher = MessageDispatcher(
        db,
        rate_limit_per_second=50,
        api_caller=caller,
    )

    await asyncio.gather(
        dispatcher.send_user("101", "one"),
        dispatcher.send_group("202", "two"),
        dispatcher.broadcast([BroadcastTarget("user", "303")], "three"),
    )

    assert len(started) == 3
    assert started[-1] - started[0] >= 0.03


async def test_separate_dispatchers_share_database_wide_rate_limit(tmp_path):
    path = str(tmp_path / "send-rate-race.db")
    first_db = Database(path)
    second_db = Database(path)
    await first_db.connect()
    await second_db.connect()
    started: list[float] = []

    async def first_caller(action: str, **params) -> dict:
        started.append(time.monotonic())
        return {"message_id": "first"}

    async def second_caller(action: str, **params) -> dict:
        started.append(time.monotonic())
        return {"message_id": "second"}

    try:
        first = MessageDispatcher(
            first_db,
            rate_limit_per_second=20,
            api_caller=first_caller,
        )
        second = MessageDispatcher(
            second_db,
            rate_limit_per_second=20,
            api_caller=second_caller,
        )

        first_result, second_result = await asyncio.gather(
            first.send_user("101", "one"),
            second.send_user("202", "two"),
        )

        assert first_result.success is True
        assert second_result.success is True
        assert len(started) == 2
        assert max(started) - min(started) >= 0.04
    finally:
        await first_db.close()
        await second_db.close()


async def test_waiting_send_stops_after_concurrent_rate_limit_failure(db):
    entered = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def caller(action: str, **params) -> dict:
        target_id = str(params.get("user_id") or params.get("group_id"))
        calls.append(target_id)
        if len(calls) == 1:
            entered.set()
            await release.wait()
            raise RuntimeError("429 rate limit")
        return {"message_id": target_id}

    dispatcher = MessageDispatcher(
        db,
        rate_limit_per_second=100,
        api_caller=caller,
        rate_limit_pause_seconds=60,
    )

    first = asyncio.create_task(dispatcher.send_user("101", "one"))
    await entered.wait()
    second = asyncio.create_task(dispatcher.send_user("202", "two"))
    await asyncio.sleep(0)
    assert second.done() is False

    release.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert first_result.success is False
    assert first_result.error == "RateLimit"
    assert second_result.success is False
    assert "暂停" in (second_result.error or "")
    assert calls == ["101"]


async def test_broadcast_empty_targets(db):
    d = MessageDispatcher(db, rate_limit_per_second=0, api_caller=_fake_caller(set()))
    report = await d.broadcast([], "msg")
    assert report.success_count == 0 and report.failed_count == 0
    assert report.skipped_count == 0


async def test_broadcast_reports_unattempted_targets_after_rate_limit_pause(db):
    calls: list[str] = []

    async def caller(action: str, **params) -> dict:
        target_id = str(params.get("user_id") or params.get("group_id"))
        calls.append(target_id)
        raise RuntimeError("429 rate limit")

    dispatcher = MessageDispatcher(
        db,
        rate_limit_per_second=0,
        api_caller=caller,
        rate_limit_pause_seconds=60,
    )
    targets = [
        BroadcastTarget("user", "123"),
        BroadcastTarget("group", "456"),
        BroadcastTarget("user", "789"),
    ]

    report = await dispatcher.broadcast(targets, "msg")

    assert calls == ["123"]
    assert report.success_count == 0
    assert report.failed_count == 1
    assert report.skipped_count == 2
    assert report.skipped_lines() == ["group:456", "user:789"]
    assert report.summary_text().startswith("发送已停止")
    assert "未发送：2" in report.summary_text()
    assert len(await db.fetchall("SELECT * FROM send_logs")) == 1


async def test_direct_send_respects_persisted_cooldown_after_restart(db):
    calls = 0

    async def caller(action: str, **params) -> dict:
        nonlocal calls
        calls += 1
        return {"message_id": "unexpected"}

    await db.extend_outbound_pause_until("9999-01-01 00:00:00")
    dispatcher = MessageDispatcher(
        db,
        rate_limit_per_second=0,
        api_caller=caller,
    )

    result = await dispatcher.send_user("123", "should wait")

    assert result.success is False
    assert "暂停" in (result.error or "")
    assert calls == 0




async def test_send_failure_does_not_persist_upstream_exception_detail(db):
    async def caller(action: str, **params):
        raise RuntimeError("secret-token=abc123 path=/private/internal")

    dispatcher = MessageDispatcher(
        db,
        rate_limit_per_second=0,
        api_caller=caller,
    )

    result = await dispatcher.send_user("123", "hello")

    assert result.success is False
    assert result.error == "RuntimeError"
    rows = await db.fetchall("SELECT error FROM send_logs")
    assert rows == [{"error": "RuntimeError"}]
    assert "secret-token" not in rows[0]["error"]
    assert "/private/internal" not in rows[0]["error"]


@pytest.mark.parametrize("response", [None, {}, "unexpected"])
async def test_send_rejects_malformed_onebot_success_response(db, response):
    async def caller(action: str, **params):
        return response

    dispatcher = MessageDispatcher(
        db,
        rate_limit_per_second=0,
        api_caller=caller,
    )
    result = await dispatcher.send_user("123", "hello")

    assert result.success is False
    assert result.error == "ValueError"
    rows = await db.fetchall("SELECT success, error FROM send_logs")
    assert len(rows) == 1
    assert rows[0]["success"] == 0
    assert rows[0]["error"] == "ValueError"


async def test_direct_send_truncates_oversized_message_at_dispatch_boundary(db):
    seen: list[str] = []

    async def caller(action: str, **params) -> dict:
        seen.append(str(params["message"]))
        return {"message_id": 1}

    dispatcher = MessageDispatcher(
        db,
        rate_limit_per_second=0,
        api_caller=caller,
    )

    result = await dispatcher.send_user(
        "123",
        "x" * (QQ_TEXT_SAFE_LIMIT + 500),
    )

    assert result.success is True
    assert len(seen) == 1
    assert len(seen[0]) <= QQ_TEXT_SAFE_LIMIT
    assert "内容过长已截断" in seen[0]
