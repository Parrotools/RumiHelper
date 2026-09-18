from app.security.permissions import (
    PermissionService,
    notification_target_requires_admin,
)


def test_admin_allowed():
    svc = PermissionService(["123456789", "87654321"])
    assert svc.is_admin("123456789") is True
    assert svc.is_admin(87654321) is True  # 数字自动转 str


def test_normal_user_denied():
    svc = PermissionService(["123456789"])
    assert svc.is_admin("11111111") is False
    assert svc.is_admin("") is False


def test_empty_admin_list():
    svc = PermissionService([])
    assert svc.is_admin("123456789") is False


def test_notification_target_requires_admin_except_self_private_target():
    assert notification_target_requires_admin("20001", "user", "20001") is False
    assert notification_target_requires_admin("20001", "user", "20002") is True
    assert notification_target_requires_admin("20001", "group", "30000") is True
