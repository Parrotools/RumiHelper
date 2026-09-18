"""权限服务：管理员判定。普通聊天/网页总结人人可用，发送类操作仅管理员。"""

from collections.abc import Iterable


def shared_session_clear_requires_admin(session_key: str) -> bool:
    """Shared group context is cross-user state, so clearing it is admin-only."""
    parts = str(session_key).split(":")
    return len(parts) == 2 and parts[0] == "group" and bool(parts[1])


def notification_target_requires_admin(
    actor_id: str,
    target_type: str,
    target_id: str,
) -> bool:
    """普通用户只能向自己的私聊配置主动通知目标。"""
    return target_type == "group" or (
        target_type == "user" and str(target_id) != str(actor_id)
    )


class PermissionService:
    def __init__(self, admin_ids: Iterable[str]):
        self._admin_ids = frozenset(str(x) for x in admin_ids)

    def is_admin(self, user_id: str) -> bool:
        return str(user_id) in self._admin_ids

    @property
    def admin_ids(self) -> frozenset[str]:
        return self._admin_ids
