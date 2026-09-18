"""pending_broadcast 生命周期测试：TTL 过期、管理员隔离、防重复执行。"""

import asyncio

import pytest

from rumi_helper.database.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    await d.connect()
    yield d
    await d.close()


async def test_create_and_get_active(db):
    pid = await db.create_pending_broadcast("admin1", [{"type": "user", "id": "123"}], "hello", ttl_seconds=300)
    pending = await db.get_active_pending("admin1")
    assert pending is not None and pending["id"] == pid
    assert pending["targets"] == [{"type": "user", "id": "123"}]
    assert pending["content"] == "hello"


async def test_admin_isolation(db):
    await db.create_pending_broadcast("admin1", [], "hello", ttl_seconds=300)
    assert await db.get_active_pending("admin2") is None


async def test_ttl_expiry(db):
    await db.create_pending_broadcast("admin1", [], "hello", ttl_seconds=-1)  # 立即过期
    assert await db.get_active_pending("admin1") is None
    # 过期后状态被标记
    rows = await db.fetchall("SELECT status FROM pending_broadcasts")
    assert rows[0]["status"] == "expired"


async def test_finish_prevents_double_execute(db):
    pid = await db.create_pending_broadcast("admin1", [], "hello", ttl_seconds=300)
    assert await db.claim_pending(pid) is True
    assert await db.finish_pending(pid, "confirmed") is True
    # 第二次领取/确认都失败（防重复执行）
    assert await db.claim_pending(pid) is False
    assert await db.finish_pending(pid, "confirmed") is False
    assert await db.get_active_pending("admin1") is None


async def test_new_pending_cancels_old(db):
    """同一管理员重复 /broadcast 时，旧的待确认任务应被自动作废。"""
    await db.create_pending_broadcast("admin1", [], "first", ttl_seconds=300)
    await db.create_pending_broadcast("admin1", [], "second", ttl_seconds=300)
    pending = await db.get_active_pending("admin1")
    assert pending is not None and pending["content"] == "second"
    rows = await db.fetchall("SELECT status FROM pending_broadcasts ORDER BY id")
    assert [r["status"] for r in rows] == ["cancelled", "active"]


async def test_cancel(db):
    pid = await db.create_pending_broadcast("admin1", [], "hello", ttl_seconds=300)
    assert await db.finish_pending(pid, "cancelled") is True
    assert await db.get_active_pending("admin1") is None


async def test_executing_broadcast_cannot_be_cancelled(db):
    pid = await db.create_pending_broadcast("admin1", [], "hello", ttl_seconds=300)
    assert await db.claim_pending(pid) is True
    assert await db.finish_pending(pid, "cancelled") is False
    assert await db.finish_pending(pid, "failed") is True


async def test_concurrent_claim_has_single_winner(db):
    pid = await db.create_pending_broadcast("admin1", [], "hello", ttl_seconds=300)
    results = await asyncio.gather(
        db.claim_pending(pid),
        db.claim_pending(pid),
    )
    assert sorted(results) == [False, True]


async def test_concurrent_replacement_leaves_only_one_active(db):
    async def create(content: str) -> int:
        return await db.create_pending_broadcast(
            "admin1",
            [{"type": "user", "id": content}],
            content,
            ttl_seconds=300,
        )

    await asyncio.gather(create("first"), create("second"))

    rows = await db.fetchall(
        "SELECT content, status FROM pending_broadcasts ORDER BY id",
        (),
    )
    assert [row["status"] for row in rows].count("active") == 1
    assert [row["status"] for row in rows].count("cancelled") == 1
    cancelled = next(row for row in rows if row["status"] == "cancelled")
    active = next(row for row in rows if row["status"] == "active")
    assert cancelled["content"] == ""
    assert active["content"] in {"first", "second"}


async def test_recover_executing_broadcasts_only_fails_expired_executions(db):
    expired_id = await db.create_pending_broadcast(
        "admin1",
        [{"type": "user", "id": "123"}],
        "expired",
        ttl_seconds=300,
    )
    assert await db.claim_pending(expired_id) is True
    await db.execute(
        "UPDATE pending_broadcasts SET expires_at = ? WHERE id = ?",
        ("2001-01-01 00:00:00", expired_id),
    )

    active_id = await db.create_pending_broadcast(
        "admin2",
        [{"type": "user", "id": "456"}],
        "active",
        ttl_seconds=300,
    )
    fresh_id = await db.create_pending_broadcast(
        "admin3",
        [{"type": "user", "id": "789"}],
        "fresh",
        ttl_seconds=300,
    )
    assert await db.claim_pending(fresh_id) is True
    await db.execute(
        "UPDATE pending_broadcasts SET expires_at = ? WHERE id = ?",
        ("9999-01-01 00:00:00", fresh_id),
    )

    assert await db.recover_executing_broadcasts("2002-01-01 00:00:00") == 1

    rows = await db.fetchall(
        "SELECT id, status FROM pending_broadcasts ORDER BY id",
        (),
    )
    assert rows == [
        {"id": expired_id, "status": "failed"},
        {"id": active_id, "status": "active"},
        {"id": fresh_id, "status": "executing"},
    ]
