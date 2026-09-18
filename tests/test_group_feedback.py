from types import SimpleNamespace

import pytest

from rumi_helper.database.db import Database
from rumi_helper.plugins import group_feedback
from rumi_helper.services.group_feedback import GroupFeedbackService


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "group-feedback.db"))
    await database.connect()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_group_feedback_defaults_to_enabled_and_persists(db):
    service = GroupFeedbackService(db)

    assert await service.is_enabled("30000") is True
    assert await service.set_enabled("30000", False) is False
    assert await service.is_enabled("30000") is False
    assert await service.set_enabled("30000", True) is True
    assert await service.is_enabled("30000") is True


def test_parse_group_feedback_command_aliases():
    assert group_feedback.parse_group_feedback_command("/group-feedback on") == "on"
    assert group_feedback.parse_group_feedback_command("/group-feedback off") == "off"
    assert group_feedback.parse_group_feedback_command("/群聊反馈 关闭") == "off"
    assert group_feedback.parse_group_feedback_command("/群聊反馈 状态") == "status"


def test_parse_group_feedback_command_rejects_invalid_text():
    with pytest.raises(ValueError):
        group_feedback.parse_group_feedback_command("/group-feedback maybe")


@pytest.mark.asyncio
async def test_silent_group_rule_blocks_only_non_admin(monkeypatch):
    runtime = SimpleNamespace(
        permission=SimpleNamespace(is_admin=lambda user_id: user_id == "admin"),
        group_feedback=SimpleNamespace(),
    )

    async def disabled():
        return False

    async def enabled():
        return True

    monkeypatch.setattr(group_feedback, "get_runtime", lambda: runtime)
    runtime.group_feedback.is_enabled = lambda _group_id: disabled()

    # Rule 使用 isinstance，因此这里用测试中的真实 OneBot 事件替换。
    from tests.test_plugin_rules import _group_event

    assert await group_feedback._silent_group_rule(_group_event("@Rumi hi", at=True, user=20001, mid=900)) is True
    assert await group_feedback._silent_group_rule(_group_event("@Rumi hi", at=True, user=20002, mid=901)) is True

    runtime.permission.is_admin = lambda user_id: user_id == "20002"
    assert await group_feedback._silent_group_rule(_group_event("@Rumi hi", at=True, user=20002, mid=902)) is False

    runtime.group_feedback.is_enabled = lambda _group_id: enabled()
    assert await group_feedback._silent_group_rule(_group_event("@Rumi hi", at=True, user=20001, mid=903)) is False
