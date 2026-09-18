"""Database methods for PendingBroadcastRepository."""

import json
from datetime import UTC, datetime, timedelta

from rumi_helper.database.repositories.common import _now

_PENDING_ACTIVE = "active"
_PENDING_EXECUTING = "executing"
_PENDING_CONFIRMED = "confirmed"
_PENDING_FAILED = "failed"
_PENDING_CANCELLED = "cancelled"
_PENDING_EXPIRED = "expired"


class PendingBroadcastRepository:
    async def create_pending_broadcast(
        self,
        admin_id: str,
        targets: list[dict],
        content: str,
        ttl_seconds: int,
    ) -> int:
        """Replace an admin's pending broadcast atomically."""
        async with self.operation():
            expires_at = (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            try:
                await self.conn.execute(
                    "UPDATE pending_broadcasts SET status = ? WHERE admin_id = ? AND status = ?",
                    (_PENDING_CANCELLED, admin_id, _PENDING_ACTIVE),
                )
                cursor = await self.conn.execute(
                    "INSERT INTO pending_broadcasts "
                    "(admin_id, targets_json, content, status, expires_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        admin_id,
                        json.dumps(targets, ensure_ascii=False),
                        content,
                        _PENDING_ACTIVE,
                        expires_at,
                    ),
                )
                await self.conn.commit()
            except Exception:
                await self.conn.rollback()
                raise
            return int(cursor.lastrowid)

    async def get_active_pending(self, admin_id: str) -> dict | None:
        """取该管理员最新的 active pending；已过期的顺手标记为 expired。"""
        async with self.operation():
            row = await self.fetchone(
                "SELECT * FROM pending_broadcasts WHERE admin_id = ? AND status = ? ORDER BY id DESC LIMIT 1",
                (admin_id, _PENDING_ACTIVE),
            )
            if row is None:
                return None
            if row["expires_at"] <= _now():
                await self.conn.execute(
                    "UPDATE pending_broadcasts SET status = ? WHERE id = ? AND status = ?",
                    (_PENDING_EXPIRED, row["id"], _PENDING_ACTIVE),
                )
                await self.conn.commit()
                return None
            row["targets"] = json.loads(row["targets_json"])
            return row

    async def claim_pending(self, pending_id: int) -> bool:
        """Atomically claim an active broadcast for execution."""
        async with self.operation(), self.conn.execute(
            "UPDATE pending_broadcasts SET status = ? WHERE id = ? AND status = ?",
            (_PENDING_EXECUTING, pending_id, _PENDING_ACTIVE),
        ) as cursor:
            await self.conn.commit()
            return cursor.rowcount > 0

    async def recover_executing_broadcasts(self, now: str) -> int:
        """Fail only expired executions; fresh ones may still belong to another live process."""
        async with self.operation(), self.conn.execute(
            "UPDATE pending_broadcasts SET status = ? "
            "WHERE status = ? AND expires_at <= ?",
            (_PENDING_FAILED, _PENDING_EXECUTING, str(now)),
        ) as cursor:
            await self.conn.commit()
            return cursor.rowcount

    async def finish_pending(self, pending_id: int, status: str) -> bool:
        """Move a pending broadcast into a valid terminal state."""
        previous = {
            _PENDING_CANCELLED: _PENDING_ACTIVE,
            _PENDING_EXPIRED: _PENDING_ACTIVE,
            _PENDING_CONFIRMED: _PENDING_EXECUTING,
            _PENDING_FAILED: _PENDING_EXECUTING,
        }.get(status)
        if previous is None:
            raise ValueError("无效的 pending broadcast 状态")
        async with self.operation(), self.conn.execute(
            "UPDATE pending_broadcasts SET status = ? WHERE id = ? AND status = ?",
            (status, pending_id, previous),
        ) as cursor:
            await self.conn.commit()
            return cursor.rowcount > 0
