"""日报查询命令。"""

from nonebot import on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent
from nonebot.rule import Rule

from rumi_helper.plugins.chat_message_utils import (
    claim_event,
    command_is_addressed,
    strip_bot_mention,
)
from rumi_helper.services.report import ReportError
from rumi_helper.services.runtime import get_runtime
from rumi_helper.utils import send_local_reply

_REPORT_COMMANDS = ("/report", "/日报")


def parse_report_command(text: str) -> bool:
    value = text.strip().lower()
    return value in _REPORT_COMMANDS or any(value.startswith(f"{command} ") for command in _REPORT_COMMANDS)


async def _report_rule(event: MessageEvent) -> bool:
    if str(event.user_id) == str(event.self_id):
        return False
    if not command_is_addressed(event):
        return False
    if parse_report_command(strip_bot_mention(event.message.extract_plain_text())):
        return claim_event(event)
    return False


report_matcher = on_message(rule=Rule(_report_rule), priority=6, block=True)


@report_matcher.handle()
async def _handle_report(event: MessageEvent):
    runtime = get_runtime()
    if isinstance(event, GroupMessageEvent):
        await send_local_reply(
            report_matcher,
            runtime,
            "为了隐私，日报只能在私聊中查看。",
        )
        return
    try:
        content = await runtime.report.build_daily_report(str(event.user_id))
    except ReportError as e:
        await send_local_reply(report_matcher, runtime, f"日报生成失败：{e}")
        return
    await send_local_reply(report_matcher, runtime, content)
