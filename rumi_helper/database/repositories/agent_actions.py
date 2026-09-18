"""Database methods for AgentActionRepository."""


class AgentActionRepository:
    async def create_pending_agent_action(
        self,
        user_id: str,
        session_key: str,
        tool_name: str,
        arguments: str,
        summary: str,
        expires_at: str,
    ) -> int:
        async with self.operation():
            try:
                await self.conn.execute(
                    "UPDATE pending_agent_actions SET status = 'cancelled' "
                    "WHERE user_id = ? AND session_key = ? AND status = 'pending'",
                    (str(user_id), str(session_key)),
                )
                cursor = await self.conn.execute(
                    "INSERT INTO pending_agent_actions "
                    "(user_id, session_key, tool_name, arguments, summary, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        str(user_id),
                        str(session_key),
                        str(tool_name),
                        arguments,
                        summary[:1000],
                        expires_at,
                    ),
                )
                await self.conn.commit()
            except Exception:
                await self.conn.rollback()
                raise
            return int(cursor.lastrowid)

    async def fetch_pending_agent_action(
        self,
        user_id: str,
        session_key: str,
        now: str,
    ) -> dict | None:
        async with self.operation():
            return await self._fetch_pending_agent_action_unlocked(
                user_id,
                session_key,
                now,
            )

    async def _fetch_pending_agent_action_unlocked(
        self,
        user_id: str,
        session_key: str,
        now: str,
    ) -> dict | None:
        await self.conn.execute(
            "UPDATE pending_agent_actions SET status = 'expired' "
            "WHERE user_id = ? AND session_key = ? AND status = 'pending' AND expires_at <= ?",
            (str(user_id), str(session_key), now),
        )
        await self.conn.commit()
        return await self.fetchone(
            "SELECT * FROM pending_agent_actions "
            "WHERE user_id = ? AND session_key = ? AND status = 'pending' "
            "ORDER BY id DESC LIMIT 1",
            (str(user_id), str(session_key)),
        )

    async def claim_pending_agent_action(
        self,
        user_id: str,
        session_key: str,
        now: str,
    ) -> dict | None:
        async with self.operation():
            pending = await self._fetch_pending_agent_action_unlocked(
                user_id,
                session_key,
                now,
            )
            if pending is None:
                return None
            async with self.conn.execute(
                "UPDATE pending_agent_actions SET status = 'executing' "
                "WHERE id = ? AND status = 'pending'",
                (pending["id"],),
            ) as cursor:
                await self.conn.commit()
                if cursor.rowcount != 1:
                    return None
            pending["status"] = "executing"
            return pending

    async def recover_executing_agent_actions(self, now: str) -> int:
        """Fail only expired executions; fresh ones may still belong to another live process."""
        async with self.operation(), self.conn.execute(
            "UPDATE pending_agent_actions SET status = 'failed' "
            "WHERE status = 'executing' AND expires_at <= ?",
            (str(now),),
        ) as cursor:
            await self.conn.commit()
            return cursor.rowcount

    async def finish_pending_agent_action(self, action_id: int, status: str) -> bool:
        allowed_from = {
            "confirmed": "executing",
            "failed": "executing",
            "cancelled": "pending",
            "expired": "pending",
        }
        previous_status = allowed_from.get(status)
        if previous_status is None:
            raise ValueError("无效的 agent action 状态")
        async with self.operation(), self.conn.execute(
            "UPDATE pending_agent_actions SET status = ? WHERE id = ? AND status = ?",
            (status, int(action_id), previous_status),
        ) as cursor:
            await self.conn.commit()
            return cursor.rowcount == 1
