"""自然语言工具调用确认入口。"""

from nonebot import on_message
from nonebot.adapters.onebot.v11 import MessageEvent
from nonebot.rule import Rule

from rumi_helper.plugins.chat_message_utils import (
    claim_event,
    command_is_addressed,
    strip_bot_mention,
)
from rumi_helper.services.runtime import get_runtime
from rumi_helper.utils import send_local_reply

_CONFIRM_WORDS = frozenset({"确认", "确认执行", "执行", "/agent confirm"})
_CANCEL_WORDS = frozenset({"取消", "取消执行", "不要执行", "/agent cancel"})


async def _agent_confirmation_rule(event: MessageEvent) -> bool:
    if str(event.user_id) == str(event.self_id) or not command_is_addressed(event):
        return False
    text = strip_bot_mention(event.message.extract_plain_text()).strip().lower()
    if text not in _CONFIRM_WORDS | _CANCEL_WORDS:
        return False
    if text in {"/agent confirm", "/agent cancel"}:
        return claim_event(event)
    try:
        runtime = get_runtime()
        if not await runtime.agent.has_pending(runtime=runtime, event=event):
            return False
    except RuntimeError:
        return False
    return claim_event(event)


agent_confirmation_matcher = on_message(rule=Rule(_agent_confirmation_rule), priority=4, block=True)


@agent_confirmation_matcher.handle()
async def _handle_agent_confirmation(event: MessageEvent):
    runtime = get_runtime()
    text = strip_bot_mention(event.message.extract_plain_text()).strip().lower()
    if text in _CONFIRM_WORDS:
        response = await runtime.agent.confirm(runtime=runtime, event=event)
    else:
        response = await runtime.agent.cancel(runtime=runtime, event=event)
    await send_local_reply(agent_confirmation_matcher, runtime, response)
