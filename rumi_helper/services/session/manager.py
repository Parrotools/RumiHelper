"""会话管理：session key 规则 + 上下文读写 + 截断。

session key 规则：
- 私聊：private:{user_id}
- 群聊（默认，成员独立上下文）：group:{group_id}:{user_id}
- 群聊（GROUP_SHARED_CONTEXT=true，全群共享）：group:{group_id}
"""

import asyncio
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from rumi_helper.database.db import Database

_TRUNCATION_MARKER = "\n…[上下文已截断]…\n"


class SessionManager:
    def __init__(
        self,
        db: Database,
        max_context_messages: int = 20,
        max_context_chars: int = 16000,
    ):
        self._db = db
        self._max = max(2, int(max_context_messages))
        self._max_chars = max(1, int(max_context_chars))
        self._turn_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    @asynccontextmanager
    async def turn(self, session_key: str) -> AsyncIterator[None]:
        """Serialize one complete conversational turn per session."""
        lock = self._turn_locks.setdefault(session_key, asyncio.Lock())
        async with lock:
            yield

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
            selected.append({"role": row["role"], "content": content})
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
        async with self._db.operation():
            try:
                await self._db.conn.executemany(
                    "INSERT INTO messages (session_key, role, content) VALUES (?, ?, ?)",
                    [
                        (session_key, role, self._truncate_message(str(content)))
                        for role, content in messages
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
