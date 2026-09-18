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
        del content
        async with self.operation():
            await self.conn.execute(
                "INSERT INTO reports (owner_id, report_type, period_start, period_end, content) "
                "VALUES (?, ?, ?, ?, '') "
                "ON CONFLICT(owner_id, report_type, period_start) DO UPDATE SET "
                "period_end = excluded.period_end, content = ''",
                (owner_id, report_type, period_start, period_end),
            )
            await self.conn.commit()
            row = await self.fetchone(
                "SELECT id FROM reports WHERE owner_id = ? AND report_type = ? AND period_start = ?",
                (owner_id, report_type, period_start),
            )
            return int(row["id"])

    async def save_reports_and_enqueue_users(
        self,
        reports: list[dict],
        max_attempts: int,
    ) -> int:
        """Atomically persist a report batch and queue each newly-created delivery once."""
        if not reports:
            return 0

        queued = 0
        next_attempt_at = _now()
        async with self.operation():
            try:
                for item in reports:
                    owner_id = str(item["owner_id"])
                    report_type = str(item["report_type"])
                    period_start = str(item["period_start"])
                    period_end = str(item["period_end"])
                    delivery_content = str(item["delivery_content"])

                    async with self.conn.execute(
                        "INSERT INTO reports "
                        "(owner_id, report_type, period_start, period_end, content) "
                        "VALUES (?, ?, ?, ?, '') "
                        "ON CONFLICT(owner_id, report_type, period_start) DO NOTHING",
                        (owner_id, report_type, period_start, period_end),
                    ) as cursor:
                        created = cursor.rowcount == 1

                    if created:
                        await self.conn.execute(
                            "INSERT INTO outbound_messages "
                            "(target_type, target_id, content, max_attempts, next_attempt_at) "
                            "VALUES ('user', ?, ?, ?, ?)",
                            (
                                owner_id,
                                delivery_content,
                                max(1, int(max_attempts)),
                                next_attempt_at,
                            ),
                        )
                        queued += 1
                    else:
                        await self.conn.execute(
                            "UPDATE reports SET period_end = ?, content = '' "
                            "WHERE owner_id = ? AND report_type = ? AND period_start = ?",
                            (period_end, owner_id, report_type, period_start),
                        )

                await self.conn.commit()
                return queued
            except Exception:
                await self.conn.rollback()
                raise

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
        """Compatibility wrapper for a single report/outbound transaction."""
        queued = await self.save_reports_and_enqueue_users(
            [
                {
                    "owner_id": owner_id,
                    "report_type": report_type,
                    "period_start": period_start,
                    "period_end": period_end,
                    "content": content,
                    "delivery_content": delivery_content,
                }
            ],
            max_attempts,
        )
        row = await self.fetchone(
            "SELECT id FROM reports WHERE owner_id = ? AND report_type = ? AND period_start = ?",
            (owner_id, report_type, period_start),
        )
        return int(row["id"]), queued == 1
