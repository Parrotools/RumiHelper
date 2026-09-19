import asyncio
import sqlite3

import aiosqlite
import pytest

import rumi_helper.database.db as db_module
from rumi_helper.database.db import Database
from rumi_helper.database.models import AGENT_ACTION_SCHEMA, OUTBOUND_MESSAGES_SCHEMA, SCHEMA


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
        assert await upgraded.migration_versions() == list(range(1, 13))
        columns = await upgraded.fetchall("PRAGMA table_info(outbound_messages)", ())
        assert "lease_token" in {str(column["name"]) for column in columns}
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
    expected = list(range(1, 13))
    for index in range(10):
        path = str(tmp_path / f"concurrent-connect-{index}.db")
        first = Database(path)
        second = Database(path)

        await asyncio.gather(first.connect(), second.connect())
        try:
            assert await first.migration_versions() == expected
            assert await second.migration_versions() == expected
            assert await first.fetchone(
                "SELECT COUNT(*) AS count FROM schema_migrations",
                (),
            ) == {"count": len(expected)}
        finally:
            await first.close()
            await second.close()


async def test_delivery_content_redaction_migration_clears_historical_terminal_bodies(tmp_path):
    path = str(tmp_path / "delivery-redaction.db")
    legacy = await aiosqlite.connect(path)
    try:
        await legacy.executescript(SCHEMA)
        await legacy.executescript(OUTBOUND_MESSAGES_SCHEMA)
        await legacy.execute(
            "INSERT INTO send_logs "
            "(target_type, target_id, content, success, message_id) "
            "VALUES ('user', '123', 'secret audit body', 1, 'm1')"
        )
        await legacy.execute(
            "INSERT INTO outbound_messages "
            "(target_type, target_id, content, status, max_attempts, next_attempt_at) "
            "VALUES ('user', '123', 'sent secret', 'sent', 3, '2000-01-01 00:00:00')"
        )
        await legacy.execute(
            "INSERT INTO outbound_messages "
            "(target_type, target_id, content, status, max_attempts, next_attempt_at) "
            "VALUES ('user', '456', 'failed secret', 'failed', 3, '2000-01-01 00:00:00')"
        )
        await legacy.execute(
            "INSERT INTO outbound_messages "
            "(target_type, target_id, content, status, max_attempts, next_attempt_at) "
            "VALUES ('user', '789', 'still needed', 'retry', 3, '2000-01-01 00:00:00')"
        )
        await legacy.commit()
    finally:
        await legacy.close()

    database = Database(path)
    await database.connect()
    try:
        assert await database.fetchall(
            "SELECT content FROM send_logs",
            (),
        ) == [{"content": ""}]
        rows = await database.fetchall(
            "SELECT status, content FROM outbound_messages ORDER BY id",
            (),
        )
        assert rows == [
            {"status": "sent", "content": ""},
            {"status": "failed", "content": ""},
            {"status": "retry", "content": "still needed"},
        ]
    finally:
        await database.close()


async def test_terminal_confirmation_redaction_migration_keeps_only_live_payloads(tmp_path):
    path = str(tmp_path / "confirmation-redaction.db")
    legacy = await aiosqlite.connect(path)
    try:
        await legacy.executescript(SCHEMA)
        await legacy.executescript(AGENT_ACTION_SCHEMA)
        await legacy.execute(
            "INSERT INTO pending_agent_actions "
            "(user_id, session_key, tool_name, arguments, summary, status, expires_at) "
            "VALUES ('u1', 'private:u1', 'memory_save', '{\"content\":\"secret\"}', "
            "'secret summary', 'confirmed', '9999-01-01T00:00:00+00:00')"
        )
        await legacy.execute(
            "INSERT INTO pending_agent_actions "
            "(user_id, session_key, tool_name, arguments, summary, status, expires_at) "
            "VALUES ('u2', 'private:u2', 'memory_save', '{\"content\":\"needed\"}', "
            "'needed summary', 'pending', '9999-01-01T00:00:00+00:00')"
        )
        await legacy.execute(
            "INSERT INTO pending_broadcasts "
            "(admin_id, targets_json, content, status, expires_at) "
            "VALUES ('a1', '[{\"type\":\"user\",\"id\":\"123\"}]', "
            "'secret broadcast', 'failed', '9999-01-01 00:00:00')"
        )
        await legacy.execute(
            "INSERT INTO pending_broadcasts "
            "(admin_id, targets_json, content, status, expires_at) "
            "VALUES ('a2', '[{\"type\":\"user\",\"id\":\"456\"}]', "
            "'needed broadcast', 'active', '9999-01-01 00:00:00')"
        )
        await legacy.commit()
    finally:
        await legacy.close()

    database = Database(path)
    await database.connect()
    try:
        actions = await database.fetchall(
            "SELECT status, arguments, summary FROM pending_agent_actions ORDER BY id",
            (),
        )
        assert actions == [
            {"status": "confirmed", "arguments": "{}", "summary": ""},
            {
                "status": "pending",
                "arguments": '{"content":"needed"}',
                "summary": "needed summary",
            },
        ]
        broadcasts = await database.fetchall(
            "SELECT status, targets_json, content FROM pending_broadcasts ORDER BY id",
            (),
        )
        assert broadcasts == [
            {"status": "failed", "targets_json": "[]", "content": ""},
            {
                "status": "active",
                "targets_json": '[{"type":"user","id":"456"}]',
                "content": "needed broadcast",
            },
        ]
    finally:
        await database.close()


async def test_report_content_redaction_migration_clears_historical_report_bodies(tmp_path):
    path = str(tmp_path / "report-redaction.db")
    legacy = await aiosqlite.connect(path)
    try:
        await legacy.executescript(SCHEMA)
        await legacy.execute(
            "INSERT INTO reports "
            "(owner_id, report_type, period_start, period_end, content) "
            "VALUES ('u1', 'daily', '2030-01-01 00:00:00', "
            "'2030-01-02 00:00:00', 'private report body')"
        )
        await legacy.commit()
    finally:
        await legacy.close()

    database = Database(path)
    await database.connect()
    try:
        assert await database.fetchone(
            "SELECT content FROM reports",
            (),
        ) == {"content": ""}
    finally:
        await database.close()
