import asyncio
import sqlite3

import aiosqlite
import pytest

import rumi_helper.database.db as db_module
from rumi_helper.database.db import Database
from rumi_helper.database.models import SCHEMA


async def test_connect_adopts_legacy_database_without_losing_data(tmp_path):
    path = str(tmp_path / "legacy.db")
    legacy = await aiosqlite.connect(path)
    try:
        await legacy.executescript(SCHEMA)
        await legacy.execute(
            "INSERT INTO memories (user_id, type, content, importance) VALUES (?, ?, ?, ?)",
            ("user-1", "fact", "保留的数据", 0.9),
        )
        await legacy.commit()
    finally:
        await legacy.close()

    upgraded = Database(path)
    await upgraded.connect()
    try:
        assert (await upgraded.fetch_memories("user-1"))[0]["content"] == "保留的数据"
        assert await upgraded.migration_versions() == [1, 2, 3, 4, 5, 6, 7, 8]
    finally:
        await upgraded.close()


async def test_shared_connection_does_not_allow_unrelated_commit_inside_transaction(tmp_path):
    db = Database(str(tmp_path / "serialized.db"))
    await db.connect()
    first_write_done = asyncio.Event()
    allow_rollback = asyncio.Event()

    async def rolled_back_transaction():
        async with db.operation():
            await db.conn.execute(
                "INSERT INTO memories (user_id, type, content, importance) VALUES (?, ?, ?, ?)",
                ("user-a", "fact", "must rollback", 0.9),
            )
            first_write_done.set()
            await allow_rollback.wait()
            await db.conn.rollback()

    async def unrelated_writer():
        await first_write_done.wait()
        task = asyncio.create_task(
            db.insert_memory("user-b", "fact", "must commit", 0.9)
        )
        await asyncio.sleep(0)
        assert task.done() is False
        allow_rollback.set()
        await task

    try:
        await asyncio.gather(rolled_back_transaction(), unrelated_writer())
        rows = await db.fetchall(
            "SELECT user_id, content FROM memories ORDER BY user_id",
            (),
        )
        assert rows == [{"user_id": "user-b", "content": "must commit"}]
    finally:
        await db.close()


async def test_database_close_waits_for_active_operation(tmp_path):
    db = Database(str(tmp_path / "close-waits.db"))
    await db.connect()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_operation():
        async with db.operation():
            entered.set()
            await release.wait()
            await db.conn.execute("SELECT 1")

    holder = asyncio.create_task(hold_operation())
    await entered.wait()
    closing = asyncio.create_task(db.close())
    await asyncio.sleep(0)

    assert closing.done() is False
    assert db._conn is not None

    release.set()
    await holder
    await closing

    assert db._conn is None


async def test_failed_migration_rolls_back_and_connect_closes_connection(
    tmp_path,
    monkeypatch,
):
    path = str(tmp_path / "failed-migration.db")
    monkeypatch.setattr(
        db_module,
        "MIGRATIONS",
        (
            (
                1,
                (
                    "CREATE TABLE atomic_a (id INTEGER);"
                    "CREATE TABLE atomic_b (id INTEGER);"
                    "THIS IS NOT VALID SQL;"
                ),
            ),
        ),
    )
    database = Database(path)

    with pytest.raises(sqlite3.OperationalError):
        await database.connect()

    assert database._conn is None

    raw = await aiosqlite.connect(path)
    try:
        async with raw.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ) as cursor:
            tables = {row[0] for row in await cursor.fetchall()}
        assert "schema_migrations" in tables
        assert "atomic_a" not in tables
        assert "atomic_b" not in tables

        async with raw.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ) as cursor:
            assert await cursor.fetchall() == []
    finally:
        await raw.close()


async def test_system_task_uniqueness_migration_disables_older_duplicates(tmp_path):
    path = str(tmp_path / "system-task-duplicates.db")
    legacy = await aiosqlite.connect(path)
    try:
        await legacy.executescript(SCHEMA)
        await legacy.execute(
            "INSERT INTO scheduled_tasks "
            "(owner_id, task_type, payload, cron_expression, next_run) "
            "VALUES ('__system__', 'github_check', '{}', '0 * * * *', '2030-01-01T01:00:00+00:00')"
        )
        await legacy.execute(
            "INSERT INTO scheduled_tasks "
            "(owner_id, task_type, payload, cron_expression, next_run) "
            "VALUES ('__system__', 'github_check', '{}', '5 * * * *', '2030-01-01T02:00:00+00:00')"
        )
        await legacy.commit()
    finally:
        await legacy.close()

    database = Database(path)
    await database.connect()
    try:
        rows = await database.fetchall(
            "SELECT id, enabled, next_run FROM scheduled_tasks "
            "WHERE owner_id = '__system__' AND task_type = 'github_check' "
            "ORDER BY id",
            (),
        )
        assert len(rows) == 2
        assert rows[0]["enabled"] == 0
        assert rows[0]["next_run"] is None
        assert rows[1]["enabled"] == 1
        assert rows[1]["next_run"] == "2030-01-01T02:00:00+00:00"

        with pytest.raises(sqlite3.IntegrityError):
            await database.execute(
                "INSERT INTO scheduled_tasks "
                "(owner_id, task_type, payload, cron_expression, next_run) "
                "VALUES ('__system__', 'github_check', '{}', '10 * * * *', "
                "'2030-01-01T03:00:00+00:00')",
                (),
            )
    finally:
        await database.close()


async def test_concurrent_connects_apply_migrations_without_lock_failure(tmp_path):
    path = str(tmp_path / "concurrent-connect.db")
    first = Database(path)
    second = Database(path)

    await asyncio.gather(first.connect(), second.connect())
    try:
        expected = list(range(1, 9))
        assert await first.migration_versions() == expected
        assert await second.migration_versions() == expected
        assert await first.fetchone(
            "SELECT COUNT(*) AS count FROM schema_migrations",
            (),
        ) == {"count": len(expected)}
    finally:
        await first.close()
        await second.close()
