"""MessageDispatcher 测试：发送、失败记录、broadcast 部分失败继续、限速间隔。"""

import time

import pytest

from rumi_helper.database.db import Database
from rumi_helper.services.qq.broadcast_parser import BroadcastTarget
from rumi_helper.services.qq.dispatcher import MessageDispatcher


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


async def test_send_user_success_logs(db):
    d = MessageDispatcher(db, rate_limit_per_second=0, api_caller=_fake_caller(set()))
    r = await d.send_user("123", "hello")
    assert r.success and r.message_id == "mid-123"
    logs = await db.fetchall("SELECT * FROM send_logs")
    assert len(logs) == 1
    assert logs[0]["success"] == 1
    assert logs[0]["target_type"] == "user"
    assert logs[0]["content"] == ""


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
    targets = [BroadcastTarget("user", str(i)) for i in range(4)]
    start = time.monotonic()
    await d.broadcast(targets, "msg")
    elapsed = time.monotonic() - start
    # 3 次间隔 * 1/50s = 0.06s，至少要有间隔（保守断言避免抖动）
    assert elapsed >= 0.045


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
    assert "message_id" in (result.error or "")
    rows = await db.fetchall("SELECT success, error FROM send_logs")
    assert len(rows) == 1
    assert rows[0]["success"] == 0
    assert "message_id" in rows[0]["error"]
