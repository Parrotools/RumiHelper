"""aiosqlite connection lifecycle plus domain repository composition.

Business SQL lives in rumi_helper.database.repositories while Database remains the
stable facade used by services and tests.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from rumi_helper.database.models import MIGRATIONS
from rumi_helper.database.repositories.agent_actions import AgentActionRepository
from rumi_helper.database.repositories.github import GitHubRepository
from rumi_helper.database.repositories.memory import MemoryRepository
from rumi_helper.database.repositories.outbound import OutboundRepository
from rumi_helper.database.repositories.pending_broadcasts import PendingBroadcastRepository
from rumi_helper.database.repositories.reports import ReportRepository
from rumi_helper.database.repositories.scheduler import SchedulerRepository
from rumi_helper.database.repositories.settings import SettingsRepository


class Database(
    OutboundRepository,
    MemoryRepository,
    SchedulerRepository,
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
            await connection.execute("PRAGMA journal_mode=WAL")
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

    async def _apply_migrations(self) -> None:
        await self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        await self.conn.commit()
        rows = await self.fetchall("SELECT version FROM schema_migrations", ())
        applied = {int(row["version"]) for row in rows}
        for version, sql in MIGRATIONS:
            if version in applied:
                continue
            # executescript() is not implicitly atomic across the whole script.
            # Start the transaction inside the script, then write the version
            # marker with a parameterized statement and commit them together.
            script = "BEGIN IMMEDIATE;\n" + sql.rstrip()
            try:
                await self.conn.executescript(script)
                await self.conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES (?)",
                    (int(version),),
                )
                await self.conn.commit()
            except Exception:
                await self.conn.rollback()
                raise

    async def migration_versions(self) -> list[int]:
        rows = await self.fetchall("SELECT version FROM schema_migrations ORDER BY version", ())
        return [int(row["version"]) for row in rows]
