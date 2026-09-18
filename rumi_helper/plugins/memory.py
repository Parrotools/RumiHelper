"""长期记忆命令：显式保存与查看，不自动保存全部聊天。"""

from nonebot import on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent
from nonebot.rule import Rule

from rumi_helper.plugins.chat_message_utils import (
    claim_event,
    command_is_addressed,
    strip_bot_mention,
)
from rumi_helper.services.memory import VALID_MEMORY_TYPES, MemoryValidationError
from rumi_helper.services.runtime import get_runtime
from rumi_helper.utils import send_local_reply

_REMEMBER = "/remember"
_LIST_COMMANDS = frozenset({"/memory", "/memories"})


def parse_remember_command(text: str) -> tuple[str, str]:
    body = text.strip()[len(_REMEMBER):].strip()
    type_part, separator, content = body.partition("--")
    if separator:
        memory_type = type_part.strip().lower() or "fact"
        content = content.strip()
    else:
        memory_type = "fact"
        content = body
    if memory_type not in VALID_MEMORY_TYPES:
        raise MemoryValidationError(f"类型应为：{', '.join(sorted(VALID_MEMORY_TYPES))}")
    if not content:
        raise MemoryValidationError("记忆内容不能为空")
    return memory_type, content


async def _remember_rule(event: MessageEvent) -> bool:
    if str(event.user_id) == str(event.self_id):
        return False
    if not command_is_addressed(event):
        return False
    text = strip_bot_mention(event.message.extract_plain_text()).lower()
    if text == _REMEMBER or text.startswith(f"{_REMEMBER} "):
        return claim_event(event)
    return False


async def _list_rule(event: MessageEvent) -> bool:
    if str(event.user_id) == str(event.self_id):
        return False
    if not command_is_addressed(event):
        return False
    if strip_bot_mention(event.message.extract_plain_text()).lower() in _LIST_COMMANDS:
        return claim_event(event)
    return False


remember_matcher = on_message(rule=Rule(_remember_rule), priority=8, block=True)
list_matcher = on_message(rule=Rule(_list_rule), priority=8, block=True)


@remember_matcher.handle()
async def _handle_remember(event: MessageEvent):
    runtime = get_runtime()
    try:
        memory_type, content = parse_remember_command(strip_bot_mention(event.message.extract_plain_text()))
        memory_id = await runtime.memory.save(str(event.user_id), memory_type, content)
    except MemoryValidationError as e:
        await send_local_reply(remember_matcher, runtime, f"格式错误：{e}")
        return
    await send_local_reply(remember_matcher, runtime, f"已保存长期记忆（{memory_type}，编号 {memory_id}）。")


@list_matcher.handle()
async def _handle_list(event: MessageEvent):
    runtime = get_runtime()
    if isinstance(event, GroupMessageEvent):
        await send_local_reply(
            list_matcher,
            runtime,
            "为了隐私，长期记忆只能在私聊中查看。",
        )
        return
    memories = await runtime.memory.list_for_context(str(event.user_id), limit=20)
    if not memories:
        await send_local_reply(list_matcher, runtime, "暂时没有长期记忆。")
        return
    lines = ["你的长期记忆："]
    lines.extend(f"{m['id']}. [{m['type']}] {m['content']}" for m in memories)
    await send_local_reply(list_matcher, runtime, "\n".join(lines))
