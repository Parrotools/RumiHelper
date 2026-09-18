import asyncio
import sqlite3

import pytest

from rumi_helper.database.db import Database
from rumi_helper.services.session.manager import SessionManager


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "test.db"))
    await d.connect()
    yield d
    await d.close()


async def test_session_keys():
    assert SessionManager.private_key("123") == "private:123"
    assert SessionManager.group_key("g1", "u1") == "group:g1:u1"
    assert SessionManager.group_key("g1", "u1", shared=True) == "group:g1"
    assert SessionManager.group_key("g1", "u2") != SessionManager.group_key("g1", "u1")


async def test_context_order_and_content(db):
    sm = SessionManager(db, max_context_messages=10)
    key = SessionManager.private_key("1")
    await sm.append(key, "user", "你好")
    await sm.append(key, "assistant", "你好！")
    ctx = await sm.get_context(key)
    assert ctx == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好！"},
    ]


async def test_trimming_to_max(db):
    sm = SessionManager(db, max_context_messages=4)
    key = SessionManager.private_key("2")
    for i in range(6):
        await sm.append(key, "user", f"m{i}")
    ctx = await sm.get_context(key)
    assert [c["content"] for c in ctx] == ["m2", "m3", "m4", "m5"]


async def test_independent_sessions(db):
    sm = SessionManager(db, max_context_messages=10)
    await sm.append(SessionManager.private_key("1"), "user", "a")
    await sm.append(SessionManager.private_key("2"), "user", "b")
    assert (await sm.get_context(SessionManager.private_key("1")))[0]["content"] == "a"
    assert (await sm.get_context(SessionManager.private_key("2")))[0]["content"] == "b"


async def test_clear(db):
    sm = SessionManager(db, max_context_messages=10)
    key = SessionManager.private_key("3")
    await sm.append(key, "user", "x")
    await sm.clear(key)
    assert await sm.get_context(key) == []


async def test_turn_serializes_same_session(db):
    sm = SessionManager(db, max_context_messages=10)
    key = SessionManager.private_key("serial")
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    order: list[str] = []

    async def first():
        async with sm.turn(key):
            order.append("first-enter")
            first_entered.set()
            await release_first.wait()
            order.append("first-exit")

    async def second():
        await first_entered.wait()
        async with sm.turn(key):
            order.append("second-enter")

    first_task = asyncio.create_task(first())
    second_task = asyncio.create_task(second())
    await first_entered.wait()
    await asyncio.sleep(0)
    assert order == ["first-enter"]

    release_first.set()
    await asyncio.gather(first_task, second_task)

    assert order == ["first-enter", "first-exit", "second-enter"]


async def test_append_rolls_back_if_session_metadata_write_fails(db):
    sm = SessionManager(db, max_context_messages=10)
    key = SessionManager.private_key("rollback-append")
    await db.execute(
        "CREATE TRIGGER fail_session_insert BEFORE INSERT ON sessions "
        "WHEN NEW.session_key = 'private:rollback-append' "
        "BEGIN SELECT RAISE(ABORT, 'blocked'); END",
        (),
    )

    with pytest.raises(sqlite3.IntegrityError):
        await sm.append(key, "user", "must not leak")

    assert await db.fetchone(
        "SELECT COUNT(*) AS count FROM messages WHERE session_key = ?",
        (key,),
    ) == {"count": 0}
    assert await db.fetchone(
        "SELECT COUNT(*) AS count FROM sessions WHERE session_key = ?",
        (key,),
    ) == {"count": 0}


async def test_clear_rolls_back_if_session_metadata_delete_fails(db):
    sm = SessionManager(db, max_context_messages=10)
    key = SessionManager.private_key("rollback-clear")
    await sm.append(key, "user", "keep me")
    await db.execute(
        "CREATE TRIGGER fail_session_delete BEFORE DELETE ON sessions "
        "WHEN OLD.session_key = 'private:rollback-clear' "
        "BEGIN SELECT RAISE(ABORT, 'blocked'); END",
        (),
    )

    with pytest.raises(sqlite3.IntegrityError):
        await sm.clear(key)

    assert await sm.get_context(key) == [{"role": "user", "content": "keep me"}]
    assert await db.fetchone(
        "SELECT COUNT(*) AS count FROM sessions WHERE session_key = ?",
        (key,),
    ) == {"count": 1}


async def test_append_turn_persists_pair_atomically(db):
    sm = SessionManager(db, max_context_messages=10)
    key = SessionManager.private_key("turn")

    await sm.append_turn(key, "question", "answer")

    assert await sm.get_context(key) == [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]


async def test_append_turn_rolls_back_user_message_if_assistant_insert_fails(db):
    sm = SessionManager(db, max_context_messages=10)
    key = SessionManager.private_key("rollback")
    await db.execute(
        "CREATE TRIGGER reject_assistant BEFORE INSERT ON messages "
        "WHEN NEW.role = 'assistant' BEGIN SELECT RAISE(ABORT, 'blocked'); END",
        (),
    )

    with pytest.raises(sqlite3.IntegrityError):
        await sm.append_turn(key, "question", "answer")

    assert await sm.get_context(key) == []
    assert await db.fetchone(
        "SELECT session_key FROM sessions WHERE session_key = ?",
        (key,),
    ) is None
