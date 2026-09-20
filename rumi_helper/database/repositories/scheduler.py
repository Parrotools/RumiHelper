"""Database methods for SchedulerRepository."""

import json

from rumi_helper.database.repositories.common import _now, _outbound_content


class SchedulerRepository:
    async def create_scheduled_task(
        self,
        owner_id: str,
        task_type: str,
        payload: dict,
        cron_expression: str | None,
        next_run: str,
        max_active_for_owner: int | None = None,
    ) -> int | None:
        async with self.operation():
            try:
                await self.conn.execute("BEGIN IMMEDIATE")
                if max_active_for_owner is not None:
                    row = await self.fetchone(
                        "SELECT COUNT(*) AS count FROM scheduled_tasks "
                        "WHERE owner_id = ? AND task_type = ? AND enabled = 1",
                        (owner_id, task_type),
                    )
                    if int(row["count"]) >= max(1, int(max_active_for_owner)):
                        await self.conn.commit()
                        return None

                async with self.conn.execute(
                    "INSERT INTO scheduled_tasks "
                    "(owner_id, task_type, payload, cron_expression, next_run) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        owner_id,
                        task_type,
                        json.dumps(payload, ensure_ascii=False),
                        cron_expression,
                        next_run,
                    ),
                ) as cursor:
                    task_id = int(cursor.lastrowid)
                await self.conn.commit()
                return task_id
            except Exception:
                await self.conn.rollback()
                raise

    async def ensure_system_scheduled_task(
        self,
        task_type: str,
        payload: dict,
        cron_expression: str,
        next_run: str,
    ) -> int:
        """Create the active system task once across concurrent Bot processes."""
        async with self.operation():
            await self.conn.execute(
                "INSERT OR IGNORE INTO scheduled_tasks "
                "(owner_id, task_type, payload, cron_expression, next_run) "
                "VALUES ('__system__', ?, ?, ?, ?)",
                (
                    str(task_type),
                    json.dumps(payload, ensure_ascii=False),
                    str(cron_expression),
                    str(next_run),
                ),
            )
            await self.conn.commit()
            row = await self.fetchone(
                "SELECT id FROM scheduled_tasks "
                "WHERE owner_id = '__system__' AND task_type = ? AND enabled = 1 "
                "ORDER BY id DESC LIMIT 1",
                (str(task_type),),
            )
            if row is None:
                raise RuntimeError("系统定时任务创建失败")
            return int(row["id"])

    async def enqueue_reminder_if_enabled(
        self,
        owner_id: str,
        content: str,
        max_attempts: int,
    ) -> bool:
        """Queue a reminder only if the user's notification flag is still enabled."""
        async with self.operation():
            try:
                async with self.conn.execute(
                    "INSERT INTO outbound_messages "
                    "(target_type, target_id, content, max_attempts, next_attempt_at) "
                    "SELECT 'user', ?, ?, ?, ? "
                    "WHERE EXISTS ("
                    "  SELECT 1 FROM notification_settings "
                    "  WHERE user_id = ? AND reminder_notify = 1"
                    ")",
                    (
                        str(owner_id),
                        _outbound_content(content),
                        max(1, int(max_attempts)),
                        _now(),
                        str(owner_id),
                    ),
                ) as cursor:
                    queued = cursor.rowcount == 1
                await self.conn.commit()
                return queued
            except Exception:
                await self.conn.rollback()
                raise

    async def prune_disabled_scheduled_tasks(self, limit: int) -> int:
        """Keep active tasks forever and bound disabled task/history retention."""
        limit = max(0, int(limit))
        async with self.operation():
            try:
                if limit == 0:
                    await self.conn.execute(
                        "DELETE FROM scheduled_content_history "
                        "WHERE task_id IN ("
                        "  SELECT id FROM scheduled_tasks WHERE enabled = 0"
                        ")"
                    )
                    async with self.conn.execute(
                        "DELETE FROM scheduled_tasks WHERE enabled = 0"
                    ) as cursor:
                        deleted = cursor.rowcount
                else:
                    params = (limit,)
                    await self.conn.execute(
                        "DELETE FROM scheduled_content_history "
                        "WHERE task_id IN ("
                        "  SELECT id FROM scheduled_tasks "
                        "  WHERE enabled = 0 AND id NOT IN ("
                        "    SELECT id FROM scheduled_tasks WHERE enabled = 0 "
                        "    ORDER BY id DESC LIMIT ?"
                        "  )"
                        ")",
                        params,
                    )
                    async with self.conn.execute(
                        "DELETE FROM scheduled_tasks "
                        "WHERE enabled = 0 AND id NOT IN ("
                        "  SELECT id FROM scheduled_tasks WHERE enabled = 0 "
                        "  ORDER BY id DESC LIMIT ?"
                        ")",
                        params,
                    ) as cursor:
                        deleted = cursor.rowcount
                await self.conn.commit()
                return max(0, int(deleted))
            except Exception:
                await self.conn.rollback()
                raise

    async def fetch_enabled_scheduled_tasks(self) -> list[dict]:
        return await self.fetchall("SELECT * FROM scheduled_tasks WHERE enabled = 1 ORDER BY id")

    async def fetch_scheduled_task(self, task_id: int) -> dict | None:
        return await self.fetchone("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,))

    async def fetch_scheduled_task_by_owner_type(self, owner_id: str, task_type: str) -> dict | None:
        return await self.fetchone(
            "SELECT * FROM scheduled_tasks WHERE owner_id = ? AND task_type = ? ORDER BY id DESC LIMIT 1",
            (owner_id, task_type),
        )

    async def claim_scheduled_task_run(
        self,
        task_id: int,
        expected_next_run: str,
        expected_cron_expression: str | None,
        claimed_at: str,
        next_run: str | None,
        enabled: bool,
    ) -> bool:
        """Atomically claim one scheduled occurrence across Bot processes."""
        async with self.operation(), self.conn.execute(
            "UPDATE scheduled_tasks "
            "SET last_run = ?, next_run = ?, enabled = ? "
            "WHERE id = ? AND enabled = 1 AND next_run = ? AND cron_expression IS ?",
            (
                claimed_at,
                next_run,
                int(enabled),
                int(task_id),
                expected_next_run,
                expected_cron_expression,
            ),
        ) as cursor:
            await self.conn.commit()
            return cursor.rowcount == 1

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
                        _outbound_content(content),
                        max(1, int(max_attempts)),
                        _now(),
                    ),
                )
                await self.conn.commit()
                return True
            except Exception:
                await self.conn.rollback()
                raise
