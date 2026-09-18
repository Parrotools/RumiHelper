"""Database methods for ReportRepository."""

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
        rows = await self.fetchall("SELECT user_id FROM notification_settings WHERE daily_report = 1 ORDER BY user_id")
        return [str(row["user_id"]) for row in rows]

    async def save_report(
        self, owner_id: str, report_type: str, period_start: str, period_end: str, content: str
    ) -> int:
        await self.execute(
            "INSERT INTO reports (owner_id, report_type, period_start, period_end, content) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(owner_id, report_type, period_start) DO UPDATE SET period_end = excluded.period_end, content = excluded.content",
            (owner_id, report_type, period_start, period_end, content),
        )
        row = await self.fetchone(
            "SELECT id FROM reports WHERE owner_id = ? AND report_type = ? AND period_start = ?",
            (owner_id, report_type, period_start),
        )
        return int(row["id"])
