"""Database methods for SettingsRepository."""

class SettingsRepository:
    async def fetch_notification_settings(self, user_id: str) -> dict | None:
        return await self.fetchone("SELECT * FROM notification_settings WHERE user_id = ?", (user_id,))

    async def save_notification_settings(
        self, user_id: str, github_notify: bool, daily_report: bool, reminder_notify: bool
    ) -> None:
        await self.execute(
            "INSERT INTO notification_settings "
            "(user_id, github_notify, daily_report, reminder_notify) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "github_notify = excluded.github_notify, "
            "daily_report = excluded.daily_report, "
            "reminder_notify = excluded.reminder_notify",
            (user_id, int(github_notify), int(daily_report), int(reminder_notify)),
        )

    async def patch_notification_settings(
        self,
        user_id: str,
        *,
        github_notify: bool | None = None,
        daily_report: bool | None = None,
        reminder_notify: bool | None = None,
    ) -> None:
        """Atomically update only supplied fields so concurrent writers cannot lose changes."""
        github_value = None if github_notify is None else int(github_notify)
        daily_value = None if daily_report is None else int(daily_report)
        reminder_value = None if reminder_notify is None else int(reminder_notify)
        await self.execute(
            "INSERT INTO notification_settings "
            "(user_id, github_notify, daily_report, reminder_notify) "
            "VALUES (?, COALESCE(?, 0), COALESCE(?, 0), COALESCE(?, 0)) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "github_notify = COALESCE(?, notification_settings.github_notify), "
            "daily_report = COALESCE(?, notification_settings.daily_report), "
            "reminder_notify = COALESCE(?, notification_settings.reminder_notify)",
            (
                user_id,
                github_value,
                daily_value,
                reminder_value,
                github_value,
                daily_value,
                reminder_value,
            ),
        )

    async def fetch_group_feedback_setting(self, group_id: str) -> dict | None:
        return await self.fetchone(
            "SELECT group_id, feedback_enabled, updated_at "
            "FROM group_feedback_settings WHERE group_id = ?",
            (str(group_id).strip(),),
        )

    async def save_group_feedback_setting(self, group_id: str, feedback_enabled: bool) -> None:
        await self.execute(
            "INSERT INTO group_feedback_settings (group_id, feedback_enabled) VALUES (?, ?) "
            "ON CONFLICT(group_id) DO UPDATE SET "
            "feedback_enabled = excluded.feedback_enabled, updated_at = datetime('now')",
            (str(group_id).strip(), int(feedback_enabled)),
        )
