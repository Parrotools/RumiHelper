"""Cross-process deduplication for inbound OneBot message events."""

import logging

from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent
from nonebot.exception import IgnoredException
from nonebot.message import event_preprocessor

from rumi_helper.plugins.chat_message_utils import event_dedup_key, is_addressed_to_bot
from rumi_helper.services.runtime import get_runtime
from rumi_helper.services.web.url_parser import extract_urls

logger = logging.getLogger(__name__)


def _group_event_may_trigger(event: GroupMessageEvent, runtime) -> bool:
    if str(event.user_id) == str(event.self_id):
        return False
    if is_addressed_to_bot(event):
        return True
    if str(runtime.settings.url_auto_summary_mode) != "all":
        return False
    return bool(extract_urls(event.message.extract_plain_text()))


@event_preprocessor
async def deduplicate_message_event(event: Event) -> None:
    """Drop replayed actionable MessageEvents before matchers produce side effects."""
    if not isinstance(event, MessageEvent):
        return

    key = event_dedup_key(event)
    try:
        runtime = get_runtime()
    except RuntimeError:
        logger.error("消息去重时 Runtime 尚未就绪 key=%s", key)
        raise IgnoredException("runtime unavailable") from None

    if isinstance(event, GroupMessageEvent) and not _group_event_may_trigger(
        event,
        runtime,
    ):
        return

    try:
        claimed = await runtime.db.claim_message_event(key)
    except Exception as exc:  # noqa: BLE001 - dedup must fail closed
        logger.error(
            "消息持久化去重失败 key=%s error=%s",
            key,
            type(exc).__name__,
        )
        raise IgnoredException("durable message dedup unavailable") from None

    if not claimed:
        raise IgnoredException("duplicate message event")
