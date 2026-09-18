"""主动通知开关，默认全部关闭。"""

from rumi_helper.database.db import Database

NOTIFICATION_FIELDS = frozenset({"github_notify", "daily_report", "reminder_notify"})


class NotificationSettingsError(ValueError):
    """通知设置不合法。"""


class NotificationSettingsService:
    def __init__(self, db: Database):
        self._db = db

    async def get(self, user_id: str) -> dict:
        user_id = str(user_id).strip()
        if not user_id:
            raise NotificationSettingsError("user_id 不能为空")
        row = await self._db.fetch_notification_settings(user_id)
        if row is None:
            return {
                "user_id": user_id,
                "github_notify": False,
                "daily_report": False,
                "reminder_notify": False,
            }
        return {field: bool(row[field]) for field in ("github_notify", "daily_report", "reminder_notify")} | {
            "user_id": user_id
        }

    async def set(self, user_id: str, **changes: bool) -> dict:
        user_id = str(user_id).strip()
        if not user_id:
            raise NotificationSettingsError("user_id 不能为空")
        unknown = set(changes) - NOTIFICATION_FIELDS
        if unknown:
            raise NotificationSettingsError(f"不支持的通知项：{', '.join(sorted(unknown))}")
        if any(not isinstance(value, bool) for value in changes.values()):
            raise NotificationSettingsError("通知开关必须是布尔值")
        await self._db.patch_notification_settings(
            user_id,
            github_notify=changes.get("github_notify"),
            daily_report=changes.get("daily_report"),
            reminder_notify=changes.get("reminder_notify"),
        )
        return await self.get(user_id)
