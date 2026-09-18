"""群聊反馈开关：保存每个群是否允许普通成员触发 Bot。"""

from app.database.db import Database


class GroupFeedbackService:
    def __init__(self, db: Database):
        self._db = db

    async def is_enabled(self, group_id: str) -> bool:
        group_id = str(group_id).strip()
        if not group_id:
            raise ValueError("group_id 不能为空")
        row = await self._db.fetch_group_feedback_setting(group_id)
        # 未配置的群保持现在的默认行为：允许反馈。
        return row is None or bool(row["feedback_enabled"])

    async def set_enabled(self, group_id: str, enabled: bool) -> bool:
        group_id = str(group_id).strip()
        if not group_id:
            raise ValueError("group_id 不能为空")
        if not isinstance(enabled, bool):
            raise TypeError("反馈开关必须是布尔值")
        await self._db.save_group_feedback_setting(group_id, enabled)
        return enabled
