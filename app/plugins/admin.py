"""管理员/通用插件：/status 查看运行状态（不含任何密钥）。"""

from nonebot import on_message
from nonebot.adapters.onebot.v11 import MessageEvent
from nonebot.matcher import Matcher
from nonebot.rule import Rule

from app.plugins.ai_chat import claim_message_id, command_is_addressed, strip_bot_mention
from app.services.health import format_status
from app.services.runtime import get_runtime
from app.utils import send_local_reply


async def _status_trigger(event: MessageEvent) -> bool:
    if str(event.user_id) == str(event.self_id):
        return False
    if not command_is_addressed(event):
        return False
    if strip_bot_mention(event.message.extract_plain_text()) in ("/status", "/状态"):
        return claim_message_id(str(event.message_id))
    return False


status_matcher = on_message(rule=Rule(_status_trigger), priority=5, block=True)


@status_matcher.handle()
async def _handle(event: MessageEvent, matcher_: Matcher):
    runtime = get_runtime()
    status = await runtime.health.check()
    await send_local_reply(
        matcher_,
        runtime,
        format_status(
            status,
            detailed=runtime.permission.is_admin(str(event.user_id)),
        ),
    )
