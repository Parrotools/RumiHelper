"""Database methods for OutboundRepository."""

from rumi_helper.database.repositories.common import _now


class OutboundRepository:
    async def enqueue_outbound_message(
        self,
        target_type: str,
        target_id: str,
        content: str,
        max_attempts: int,
        not_before: str | None = None,
    ) -> int:
        async with self.operation():
            cursor = await self.conn.execute(
                "INSERT INTO outbound_messages "
                "(target_type, target_id, content, max_attempts, next_attempt_at) VALUES (?, ?, ?, ?, ?)",
                (target_type, target_id, content, max_attempts, not_before or _now()),
            )
            await self.conn.commit()
            return int(cursor.lastrowid)

    async def claim_outbound_message(self, now: str, lease_before: str) -> dict | None:
        async with self.operation():
            async with self.conn.execute(
                "SELECT id FROM outbound_messages WHERE "
                "((status IN ('pending', 'retry') AND next_attempt_at <= ?) "
                "OR (status = 'sending' AND locked_at <= ?)) ORDER BY id LIMIT 1",
                (now, lease_before),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return None
            message_id = int(row["id"])
            async with self.conn.execute(
                "UPDATE outbound_messages SET status = 'sending', attempts = attempts + 1, locked_at = ? "
                "WHERE id = ? AND "
                "((status IN ('pending', 'retry') AND next_attempt_at <= ?) "
                "OR (status = 'sending' AND locked_at <= ?))",
                (now, message_id, now, lease_before),
            ) as cursor:
                await self.conn.commit()
                if cursor.rowcount != 1:
                    # Another process may still win the race outside this Bot process.
                    return None
            return await self.fetchone("SELECT * FROM outbound_messages WHERE id = ?", (message_id,))

    async def defer_outbound_messages(self, not_before: str) -> None:
        """Push existing queued items behind a cooldown boundary."""
        await self.execute(
            "UPDATE outbound_messages SET next_attempt_at = ? "
            "WHERE status IN ('pending', 'retry') AND next_attempt_at < ?",
            (not_before, not_before),
        )

    async def fetch_outbound_pause_until(self) -> str:
        row = await self.fetchone("SELECT paused_until FROM outbound_control WHERE id = 1", ())
        return str(row["paused_until"]) if row else "1970-01-01 00:00:00"

    async def extend_outbound_pause_until(self, paused_until: str) -> None:
        """Persist the longest known global circuit-breaker deadline."""
        await self.execute(
            "UPDATE outbound_control SET paused_until = ? "
            "WHERE id = 1 AND paused_until < ?",
            (paused_until, paused_until),
        )

    async def mark_outbound_message_sent(self, message_id: int, sent_at: str, qq_message_id: str | None) -> None:
        await self.execute(
            "UPDATE outbound_messages SET status = 'sent', sent_at = ?, message_id = ?, locked_at = NULL, "
            "last_error = NULL WHERE id = ?",
            (sent_at, qq_message_id, message_id),
        )

    async def retry_outbound_message(
        self, message_id: int, attempts: int, max_attempts: int, next_attempt_at: str, error: str
    ) -> None:
        if attempts >= max_attempts:
            await self.execute(
                "UPDATE outbound_messages SET status = 'failed', locked_at = NULL, last_error = ? WHERE id = ?",
                (error[:500], message_id),
            )
            return
        await self.execute(
            "UPDATE outbound_messages SET status = 'retry', next_attempt_at = ?, locked_at = NULL, last_error = ? "
            "WHERE id = ?",
            (next_attempt_at, error[:500], message_id),
        )

    async def outbound_message_counts(self) -> dict[str, int]:
        rows = await self.fetchall("SELECT status, COUNT(*) AS count FROM outbound_messages GROUP BY status", ())
        return {str(row["status"]): int(row["count"]) for row in rows}

    async def fetch_recent_errors(self, limit: int = 5) -> list[dict]:
        return await self.fetchall(
            "SELECT error, created_at FROM send_logs WHERE success = 0 ORDER BY id DESC LIMIT ?", (max(1, limit),)
        )

    async def insert_send_log(
        self,
        target_type: str,
        target_id: str,
        content: str,
        success: bool,
        message_id: str | None,
        error: str | None,
    ) -> None:
        await self.execute(
            "INSERT INTO send_logs (target_type, target_id, content, success, message_id, error) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (target_type, target_id, content[:500], int(success), message_id, error[:500] if error else None),
        )
