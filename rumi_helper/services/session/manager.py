"""会话管理：session key 规则 + 上下文读写 + 截断。

session key 规则：
- 私聊：private:{user_id}
- 群聊（默认，成员独立上下文）：group:{group_id}:{user_id}
- 群聊（GROUP_SHARED_CONTEXT=true，全群共享）：group:{group_id}
"""

import asyncio
import logging
import secrets
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from rumi_helper.database.db import Database

_TRUNCATION_MARKER = "\n…[上下文已截断]…\n"
_PERSISTED_ROLES = frozenset({"user", "assistant"})

logger = logging.getLogger(__name__)


class SessionManager:
    def __init__(
        self,
        db: Database,
        max_context_messages: int = 20,
        max_context_chars: int = 16000,
        turn_lease_seconds: float = 30.0,
        turn_lease_poll_seconds: float = 0.05,
    ):
        self._db = db
        self._max = max(2, int(max_context_messages))
        self._max_chars = max(1, int(max_context_chars))
        self._turn_lease_seconds = max(5.0, float(turn_lease_seconds))
        self._turn_lease_poll_seconds = max(0.01, float(turn_lease_poll_seconds))
        self._turn_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    @asynccontextmanager
    async def turn(self, session_key: str) -> AsyncIterator[None]:
        """Serialize one complete conversational turn across tasks and Bot processes."""
        lock = self._turn_locks.setdefault(session_key, asyncio.Lock())
        async with lock:
            lease_token = secrets.token_urlsafe(24)
            await self._acquire_turn_lease(session_key, lease_token)
            heartbeat_stop = asyncio.Event()
            owner_task = asyncio.current_task()
            heartbeat = asyncio.create_task(
                self._renew_turn_lease(
                    session_key,
                    lease_token,
                    heartbeat_stop,
                    owner_task,
                ),
                name=f"session-turn-lease:{session_key}",
            )
            try:
                # Protect an old-but-now-active session from retention pruning while
                # this turn is still waiting on the LLM/tool path and has not yet
                # persisted its new user/assistant pair.
                await self._db.execute(
                    "UPDATE sessions SET updated_at = datetime('now') "
                    "WHERE session_key = ?",
                    (session_key,),
                )
                yield
            finally:
                heartbeat_stop.set()
                try:
                    await heartbeat
                except Exception as exc:  # noqa: BLE001 - release must still run
                    logger.warning(
                        "session turn lease heartbeat failed session=%s error=%s",
                        session_key,
                        type(exc).__name__,
                    )
                await self._db.release_session_turn(session_key, lease_token)

    async def _acquire_turn_lease(self, session_key: str, lease_token: str) -> None:
        while True:
            now = datetime.now(UTC)
            expires_at = now + timedelta(seconds=self._turn_lease_seconds)
            if await self._db.claim_session_turn(
                session_key,
                lease_token,
                self._lease_timestamp(now),
                self._lease_timestamp(expires_at),
            ):
                return
            await asyncio.sleep(self._turn_lease_poll_seconds)

    async def _renew_turn_lease(
        self,
        session_key: str,
        lease_token: str,
        stop: asyncio.Event,
        owner_task: asyncio.Task | None,
    ) -> None:
        interval = max(1.0, self._turn_lease_seconds / 3.0)
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                expires_at = datetime.now(UTC) + timedelta(
                    seconds=self._turn_lease_seconds
                )
                renewed = await self._db.renew_session_turn(
                    session_key,
                    lease_token,
                    self._lease_timestamp(expires_at),
                )
                if not renewed:
                    logger.error(
                        "session turn lease lost session=%s",
                        session_key,
                    )
                    if owner_task is not None and not owner_task.done():
                        owner_task.cancel()
                    return

    @staticmethod
    def _lease_timestamp(value: datetime) -> str:
        return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")

    @staticmethod
    def private_key(user_id: str) -> str:
        return f"private:{user_id}"

    @staticmethod
    def group_key(group_id: str, user_id: str, shared: bool = False) -> str:
        if shared:
            return f"group:{group_id}"
        return f"group:{group_id}:{user_id}"

    async def get_context(
        self,
        session_key: str,
        *,
        reserve_chars: int = 0,
    ) -> list[dict]:
        """返回最近 N 条，并在为当前 turn 预留字符后保留最新上下文。"""
        rows = await self._db.fetchall(
            "SELECT role, content FROM ("
            "  SELECT id, role, content FROM messages WHERE session_key = ? ORDER BY id DESC LIMIT ?"
            ") ORDER BY id ASC",
            (session_key, self._max),
        )
        selected: list[dict] = []
        remaining = max(0, self._max_chars - max(0, int(reserve_chars)))
        for row in reversed(rows):
            if remaining <= 0:
                break
            content = str(row["content"])
            if len(content) > remaining:
                content = content[-remaining:]
            role = str(row["role"])
            if role not in _PERSISTED_ROLES:
                role = "user"
            selected.append({"role": role, "content": content})
            remaining -= len(content)
        selected.reverse()
        return selected

    async def prepare_turn_context(
        self,
        session_key: str,
        current_content: str,
    ) -> tuple[list[dict], str]:
        """Bound the current prompt and fit history into the remaining context budget."""
        bounded = self._truncate_message(str(current_content))
        history = await self.get_context(
            session_key,
            reserve_chars=len(bounded),
        )
        return history, bounded

    async def append(self, session_key: str, role: str, content: str) -> None:
        """Append one message and trim history atomically."""
        await self._append_messages(session_key, [(role, content)])

    async def append_turn(
        self,
        session_key: str,
        user_content: str,
        assistant_content: str,
    ) -> None:
        """Persist a complete user/assistant turn atomically."""
        await self._append_messages(
            session_key,
            [("user", user_content), ("assistant", assistant_content)],
        )

    async def _append_messages(
        self,
        session_key: str,
        messages: list[tuple[str, str]],
    ) -> None:
        normalized: list[tuple[str, str]] = []
        for role, content in messages:
            role = str(role)
            if role not in _PERSISTED_ROLES:
                raise ValueError("会话消息 role 只能是 user 或 assistant")
            normalized.append((role, self._truncate_message(str(content))))

        async with self._db.operation():
            try:
                await self._db.conn.executemany(
                    "INSERT INTO messages (session_key, role, content) VALUES (?, ?, ?)",
                    [
                        (session_key, role, content)
                        for role, content in normalized
                    ],
                )
                await self._db.conn.execute(
                    "INSERT INTO sessions (session_key) VALUES (?) "
                    "ON CONFLICT(session_key) DO UPDATE SET updated_at = datetime('now')",
                    (session_key,),
                )
                # 只保留最近 N 条，防止上下文无限增长
                await self._db.conn.execute(
                    "DELETE FROM messages WHERE session_key = ? AND id NOT IN ("
                    "  SELECT id FROM messages WHERE session_key = ? ORDER BY id DESC LIMIT ?"
                    ")",
                    (session_key, session_key, self._max),
                )
                await self._db.conn.commit()
            except Exception:
                await self._db.conn.rollback()
                raise

    def _truncate_message(self, content: str) -> str:
        if len(content) <= self._max_chars:
            return content
        if len(_TRUNCATION_MARKER) >= self._max_chars:
            return content[: self._max_chars]
        remaining = self._max_chars - len(_TRUNCATION_MARKER)
        head = remaining // 2
        tail = remaining - head
        return content[:head] + _TRUNCATION_MARKER + content[-tail:]

    async def prune_inactive(
        self,
        retention_days: int,
        *,
        now: datetime | None = None,
    ) -> int:
        """Delete stale session metadata and messages atomically."""
        days = max(0, int(retention_days))
        if days == 0:
            return 0
        current = now or datetime.now(UTC)
        cutoff = (current.astimezone(UTC) - timedelta(days=days)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        async with self._db.operation():
            try:
                await self._db.conn.execute(
                    "DELETE FROM messages WHERE session_key IN ("
                    "  SELECT session_key FROM sessions "
                    "  WHERE datetime(updated_at) < datetime(?)"
                    ")",
                    (cutoff,),
                )
                async with self._db.conn.execute(
                    "DELETE FROM sessions WHERE datetime(updated_at) < datetime(?)",
                    (cutoff,),
                ) as cursor:
                    deleted = cursor.rowcount
                await self._db.conn.commit()
                return max(0, int(deleted))
            except Exception:
                await self._db.conn.rollback()
                raise

    async def clear(self, session_key: str) -> None:
        """Delete session metadata and messages atomically."""
        async with self._db.operation():
            try:
                await self._db.conn.execute(
                    "DELETE FROM messages WHERE session_key = ?",
                    (session_key,),
                )
                await self._db.conn.execute(
                    "DELETE FROM sessions WHERE session_key = ?",
                    (session_key,),
                )
                await self._db.conn.commit()
            except Exception:
                await self._db.conn.rollback()
                raise
