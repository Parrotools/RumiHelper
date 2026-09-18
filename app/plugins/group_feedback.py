"""群聊反馈总开关。

关闭后，普通成员在该群发送的聊天和命令都会被静默拦截；Bot 管理员仍可
正常使用命令和对话。开关状态保存在 SQLite 中，重启 Bot 不会丢失。
"""

from nonebot import on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent
from nonebot.rule import Rule

from app.plugins.ai_chat import claim_message_id, command_is_addressed, strip_bot_mention
from app.services.runtime import get_runtime
from app.utils import send_local_reply

_COMMANDS = frozenset({"/group-feedback", "/群聊反馈"})
_ACTIONS = {
    "on": True,
    "开启": True,
    "开": True,
    "off": False,
    "关闭": False,
    "关": False,
}
_STATUS_WORDS = frozenset({"status", "状态", "查询"})


def _command_text(event: MessageEvent) -> str:
    return strip_bot_mention(event.message.extract_plain_text())


def parse_group_feedback_command(text: str) -> str:
    """返回 ``on``、``off`` 或 ``status``；格式不正确时抛出 ValueError。"""
    parts = strip_bot_mention(text).split()
    if len(parts) != 2 or parts[0].lower() not in _COMMANDS:
        raise ValueError("格式：/group-feedback on|off|status")
    action = parts[1].lower()
    if action in _ACTIONS:
        return "on" if _ACTIONS[action] else "off"
    if action in _STATUS_WORDS:
        return "status"
    raise ValueError("格式：/group-feedback on|off|status")


def _looks_like_command(text: str) -> bool:
    first = text.split(maxsplit=1)[0].lower() if text else ""
    return first in _COMMANDS


async def _silent_group_rule(event: MessageEvent) -> bool:
    """在所有业务 matcher 之前吞掉静默群里的普通成员消息。"""
    if not isinstance(event, GroupMessageEvent):
        return False
    if str(event.user_id) == str(event.self_id):
        return False
    runtime = get_runtime()
    if runtime.permission.is_admin(str(event.user_id)):
        return False
    return not await runtime.group_feedback.is_enabled(str(event.group_id))


silent_group_matcher = on_message(rule=Rule(_silent_group_rule), priority=1, block=True)


@silent_group_matcher.handle()
async def _handle_silent_group(_event: GroupMessageEvent):
    # 故意不发送任何消息；matcher 的 block=True 会阻止后续所有插件响应。
    return


async def _group_feedback_rule(event: MessageEvent) -> bool:
    if str(event.user_id) == str(event.self_id):
        return False
    if not command_is_addressed(event):
        return False
    if not _looks_like_command(_command_text(event)):
        return False
    return claim_message_id(str(event.message_id))


group_feedback_matcher = on_message(rule=Rule(_group_feedback_rule), priority=3, block=True)


@group_feedback_matcher.handle()
async def _handle_group_feedback(event: MessageEvent):
    runtime = get_runtime()
    if not runtime.permission.is_admin(str(event.user_id)):
        await send_local_reply(group_feedback_matcher, runtime, "该命令仅 Bot 管理员可用。")
        return
    if not isinstance(event, GroupMessageEvent):
        await send_local_reply(group_feedback_matcher, runtime, "请在目标群聊中使用此命令。")
        return

    try:
        action = parse_group_feedback_command(_command_text(event))
    except ValueError as exc:
        await send_local_reply(group_feedback_matcher, runtime, str(exc))
        return

    group_id = str(event.group_id)
    if action == "status":
        enabled = await runtime.group_feedback.is_enabled(group_id)
        state = "开启" if enabled else "关闭"
        await send_local_reply(group_feedback_matcher, runtime, f"本群普通成员反馈当前为：{state}。")
        return

    enabled = await runtime.group_feedback.set_enabled(group_id, action == "on")
    if enabled:
        message = "本群普通成员反馈已开启。"
    else:
        message = "本群普通成员反馈已关闭；普通成员的聊天和命令将不回复，Bot 管理员仍可正常使用。"
    await send_local_reply(group_feedback_matcher, runtime, message)
