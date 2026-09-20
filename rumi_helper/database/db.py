"""aiosqlite connection lifecycle plus domain repository composition.

Business SQL lives in rumi_helper.database.repositories while Database remains the
stable facade used by services and tests.
"""

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from rumi_helper.database.models import MIGRATIONS
from rumi_helper.database.repositories.agent_actions import AgentActionRepository
from rumi_helper.database.repositories.github import GitHubRepository
from rumi_helper.database.repositories.memory import MemoryRepository
from rumi_helper.database.repositories.message_events import MessageEventRepository
from rumi_helper.database.repositories.outbound import OutboundRepository
from rumi_helper.database.repositories.pending_broadcasts import PendingBroadcastRepository
from rumi_helper.database.repositories.reports import ReportRepository
from rumi_helper.database.repositories.scheduler import SchedulerRepository
from rumi_helper.database.repositories.session_turns import SessionTurnRepository
from rumi_helper.database.repositories.settings import SettingsRepository


def _split_sql_statements(script: str) -> list[str]:
    """Split a migration script without breaking trigger BEGIN/END bodies."""
    statements: list[str] = []
    buffer: list[str] = []
    for char in script:
        buffer.append(char)
        if char != ";":
            continue
        candidate = "".join(buffer).strip()
        if candidate and sqlite3.complete_statement(candidate):
            statements.append(candidate)
            buffer.clear()

    tail = "".join(buffer).strip()
    if tail:
        statements.append(tail)
    return statements


class Database(
    OutboundRepository,
    MemoryRepository,
    MessageEventRepository,
    SchedulerRepository,
    SessionTurnRepository,
    AgentActionRepository,
    SettingsRepository,
    GitHubRepository,
    ReportRepository,
    PendingBroadcastRepository,
):
    def __init__(self, path: str):
        self._path = Path(path)
        self._conn: aiosqlite.Connection | None = None
        # All services share one aiosqlite connection. Serializing operations
        # prevents an unrelated coroutine from committing another repository's
        # half-finished multi-statement transaction.
        self._operation_lock = asyncio.Lock()
        self._operation_owner: asyncio.Task | None = None
        self._operation_depth = 0

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(self._path)
        self._conn = connection
        try:
            connection.row_factory = aiosqlite.Row
            await connection.execute("PRAGMA busy_timeout=10000")
            await self._quick_check()
            await self._execute_with_lock_retry("PRAGMA journal_mode=WAL")
            await self._apply_migrations()
        except Exception:
            await connection.close()
            self._conn = None
            raise

    async def close(self) -> None:
        """Wait for the current serialized DB operation before closing the connection."""
        async with self.operation():
            if self._conn is not None:
                await self._conn.close()
                self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database 未连接，请先调用 connect()")
        return self._conn

    @asynccontextmanager
    async def operation(self) -> AsyncIterator[None]:
        """Serialize access to the shared connection; nested calls by one task are reentrant."""
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Database operation 必须运行在 asyncio Task 中")
        if self._operation_owner is task:
            self._operation_depth += 1
            try:
                yield
            finally:
                self._operation_depth -= 1
            return

        await self._operation_lock.acquire()
        self._operation_owner = task
        self._operation_depth = 1
        try:
            yield
        finally:
            self._operation_owner = None
            self._operation_depth = 0
            self._operation_lock.release()

    async def execute(self, sql: str, params: tuple = ()) -> None:
        async with self.operation():
            await self.conn.execute(sql, params)
            await self.conn.commit()

    async def fetchone(self, sql: str, params: tuple = ()) -> dict | None:
        async with self.operation():
            async with self.conn.execute(sql, params) as cursor:
                row = await cursor.fetchone()
            return dict(row) if row else None

    async def fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        async with self.operation():
            async with self.conn.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def _quick_check(self) -> None:
        async with self.conn.execute("PRAGMA quick_check") as cursor:
            rows = await cursor.fetchall()
        details = [str(row[0]) for row in rows]
        if details != ["ok"]:
            raise sqlite3.DatabaseError("SQLite quick_check failed")

    async def _execute_with_lock_retry(
        self,
        sql: str,
        params: tuple = (),
        *,
        attempts: int = 20,
    ) -> None:
        delay = 0.01
        for attempt in range(max(1, attempts)):
            try:
                cursor = await self.conn.execute(sql, params)
                await cursor.close()
                return
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                if "locked" not in message or attempt + 1 >= attempts:
                    raise
                await asyncio.sleep(delay)
                delay = min(delay * 2, 0.25)

    async def _apply_migrations(self) -> None:
        await self._execute_with_lock_retry(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        await self.conn.commit()
        rows = await self.fetchall("SELECT version FROM schema_migrations", ())
        applied = {int(row["version"]) for row in rows}
        for version, sql in MIGRATIONS:
            if version in applied:
                continue
            try:
                # Serialize migration writers across processes. Re-check after
                # acquiring the write lock because another process may have
                # applied this version while we were waiting.
                await self._execute_with_lock_retry("BEGIN IMMEDIATE")
                async with self.conn.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = ?",
                    (int(version),),
                ) as cursor:
                    already_applied = await cursor.fetchone()
                if already_applied is not None:
                    await self.conn.commit()
                    applied.add(int(version))
                    continue

                for statement in _split_sql_statements(sql):
                    await self.conn.execute(statement)
                await self.conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES (?)",
                    (int(version),),
                )
                await self.conn.commit()
                applied.add(int(version))
            except Exception:
                await self.conn.rollback()
                raise

    async def migration_versions(self) -> list[int]:
        rows = await self.fetchall("SELECT version FROM schema_migrations ORDER BY version", ())
        return [int(row["version"]) for row in rows]
