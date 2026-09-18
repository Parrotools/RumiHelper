"""Database methods for SchedulerRepository."""

import json

from rumi_helper.database.repositories.common import _now


class SchedulerRepository:
    async def create_scheduled_task(
        self,
        owner_id: str,
        task_type: str,
        payload: dict,
        cron_expression: str | None,
        next_run: str,
    ) -> int:
        async with self.operation():
            cursor = await self.conn.execute(
                "INSERT INTO scheduled_tasks "
                "(owner_id, task_type, payload, cron_expression, next_run) VALUES (?, ?, ?, ?, ?)",
                (owner_id, task_type, json.dumps(payload, ensure_ascii=False), cron_expression, next_run),
            )
            await self.conn.commit()
            return int(cursor.lastrowid)

    async def fetch_enabled_scheduled_tasks(self) -> list[dict]:
        return await self.fetchall("SELECT * FROM scheduled_tasks WHERE enabled = 1 ORDER BY id")

    async def fetch_scheduled_task(self, task_id: int) -> dict | None:
        return await self.fetchone("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,))

    async def fetch_scheduled_task_by_owner_type(self, owner_id: str, task_type: str) -> dict | None:
        return await self.fetchone(
            "SELECT * FROM scheduled_tasks WHERE owner_id = ? AND task_type = ? ORDER BY id DESC LIMIT 1",
            (owner_id, task_type),
        )

    async def mark_scheduled_task_run(
        self, task_id: int, last_run: str, next_run: str | None, enabled: bool
    ) -> None:
        await self.execute(
            "UPDATE scheduled_tasks SET last_run = ?, next_run = ?, enabled = ? WHERE id = ?",
            (last_run, next_run, int(enabled), task_id),
        )

    async def update_scheduled_task_schedule(
        self, task_id: int, cron_expression: str | None, next_run: str | None, enabled: bool
    ) -> None:
        await self.execute(
            "UPDATE scheduled_tasks SET cron_expression = ?, next_run = ?, enabled = ? WHERE id = ?",
            (cron_expression, next_run, int(enabled), task_id),
        )

    async def fetch_scheduled_content_history(self, task_id: int, limit: int = 20) -> list[str]:
        rows = await self.fetchall(
            "SELECT content FROM scheduled_content_history WHERE task_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (task_id, max(1, limit)),
        )
        return [str(row["content"]) for row in rows]

    async def insert_scheduled_content_history(self, task_id: int, content: str) -> None:
        async with self.operation():
            try:
                await self._insert_scheduled_content_history_unlocked(task_id, content)
                await self.conn.commit()
            except Exception:
                await self.conn.rollback()
                raise

    async def _insert_scheduled_content_history_unlocked(
        self,
        task_id: int,
        content: str,
    ) -> bool:
        async with self.conn.execute(
            "INSERT OR IGNORE INTO scheduled_content_history (task_id, content) VALUES (?, ?)",
            (task_id, content),
        ) as cursor:
            created = cursor.rowcount == 1
        await self.conn.execute(
            "DELETE FROM scheduled_content_history WHERE task_id = ? AND id NOT IN ("
            "  SELECT id FROM scheduled_content_history WHERE task_id = ? "
            "  ORDER BY id DESC LIMIT 20"
            ")",
            (task_id, task_id),
        )
        return created

    async def record_scheduled_content_and_enqueue_group(
        self,
        task_id: int,
        group_id: str,
        content: str,
        max_attempts: int,
    ) -> bool:
        """Atomically queue generated content only for the unique history winner."""
        async with self.operation():
            try:
                created = await self._insert_scheduled_content_history_unlocked(
                    task_id,
                    content,
                )
                if not created:
                    await self.conn.commit()
                    return False
                await self.conn.execute(
                    "INSERT INTO outbound_messages "
                    "(target_type, target_id, content, max_attempts, next_attempt_at) "
                    "VALUES ('group', ?, ?, ?, ?)",
                    (
                        str(group_id),
                        str(content),
                        max(1, int(max_attempts)),
                        _now(),
                    ),
                )
                await self.conn.commit()
                return True
            except Exception:
                await self.conn.rollback()
                raise
