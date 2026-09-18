"""管理员/通用插件：/status 查看运行状态（不含任何密钥）。"""

from nonebot import on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent
from nonebot.matcher import Matcher
from nonebot.rule import Rule

from rumi_helper.plugins.chat_message_utils import (
    claim_event,
    command_is_addressed,
    strip_bot_mention,
)
from rumi_helper.services.health import format_status
from rumi_helper.services.runtime import get_runtime
from rumi_helper.utils import send_local_reply


async def _status_trigger(event: MessageEvent) -> bool:
    if str(event.user_id) == str(event.self_id):
        return False
    if not command_is_addressed(event):
        return False
    if strip_bot_mention(event.message.extract_plain_text()) in ("/status", "/状态"):
        return claim_event(event)
    return False


status_matcher = on_message(rule=Rule(_status_trigger), priority=5, block=True)


@status_matcher.handle()
async def _handle(event: MessageEvent, matcher_: Matcher):
    runtime = get_runtime()
    detailed = (
        runtime.permission.is_admin(str(event.user_id))
        and not isinstance(event, GroupMessageEvent)
    )
    status = await runtime.health.check(include_diagnostics=detailed)
    await send_local_reply(
        matcher_,
        runtime,
        format_status(status, detailed=detailed),
    )
