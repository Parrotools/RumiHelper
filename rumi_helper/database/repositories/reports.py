"""Database methods for ReportRepository."""

from rumi_helper.database.repositories.common import _now


class ReportRepository:
    async def fetch_scheduled_tasks_for_report(
        self, owner_id: str, period_start: str, period_end: str
    ) -> list[dict]:
        return await self.fetchall(
            "SELECT * FROM scheduled_tasks WHERE owner_id = ? AND "
            "((datetime(created_at) >= datetime(?) AND datetime(created_at) < datetime(?)) "
            "OR (datetime(last_run) >= datetime(?) AND datetime(last_run) < datetime(?)) "
            "OR (datetime(next_run) >= datetime(?) AND datetime(next_run) < datetime(?))) ORDER BY id",
            (owner_id, period_start, period_end, period_start, period_end, period_start, period_end),
        )

    async def fetch_memories_for_report(self, user_id: str, period_start: str, period_end: str) -> list[dict]:
        return await self.fetchall(
            "SELECT * FROM memories WHERE user_id = ? AND datetime(updated_at) >= datetime(?) "
            "AND datetime(updated_at) < datetime(?) "
            "AND importance >= 0.8 ORDER BY importance DESC, id",
            (user_id, period_start, period_end),
        )

    async def fetch_daily_report_users(self) -> list[str]:
        rows = await self.fetchall(
            "SELECT user_id FROM notification_settings WHERE daily_report = 1 ORDER BY user_id"
        )
        return [str(row["user_id"]) for row in rows]

    async def save_report(
        self,
        owner_id: str,
        report_type: str,
        period_start: str,
        period_end: str,
        content: str,
    ) -> int:
        async with self.operation():
            await self.conn.execute(
                "INSERT INTO reports (owner_id, report_type, period_start, period_end, content) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(owner_id, report_type, period_start) DO UPDATE SET "
                "period_end = excluded.period_end, content = excluded.content",
                (owner_id, report_type, period_start, period_end, content),
            )
            await self.conn.commit()
            row = await self.fetchone(
                "SELECT id FROM reports WHERE owner_id = ? AND report_type = ? AND period_start = ?",
                (owner_id, report_type, period_start),
            )
            return int(row["id"])

    async def save_report_and_enqueue_user(
        self,
        owner_id: str,
        report_type: str,
        period_start: str,
        period_end: str,
        content: str,
        delivery_content: str,
        max_attempts: int,
    ) -> tuple[int, bool]:
        """Atomically persist a report and enqueue its first delivery exactly once.

        The reports uniqueness constraint is the idempotency key. Re-running the
        same period updates the stored content but does not create another
        outbound row.
        """
        async with self.operation():
            try:
                async with self.conn.execute(
                    "INSERT INTO reports "
                    "(owner_id, report_type, period_start, period_end, content) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(owner_id, report_type, period_start) DO NOTHING",
                    (owner_id, report_type, period_start, period_end, content),
                ) as cursor:
                    created = cursor.rowcount == 1

                if not created:
                    await self.conn.execute(
                        "UPDATE reports SET period_end = ?, content = ? "
                        "WHERE owner_id = ? AND report_type = ? AND period_start = ?",
                        (period_end, content, owner_id, report_type, period_start),
                    )

                row = await self.fetchone(
                    "SELECT id FROM reports "
                    "WHERE owner_id = ? AND report_type = ? AND period_start = ?",
                    (owner_id, report_type, period_start),
                )
                report_id = int(row["id"])

                if created:
                    await self.conn.execute(
                        "INSERT INTO outbound_messages "
                        "(target_type, target_id, content, max_attempts, next_attempt_at) "
                        "VALUES ('user', ?, ?, ?, ?)",
                        (
                            owner_id,
                            delivery_content,
                            max(1, int(max_attempts)),
                            _now(),
                        ),
                    )

                await self.conn.commit()
                return report_id, created
            except Exception:
                await self.conn.rollback()
                raise
