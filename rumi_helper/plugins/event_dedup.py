"""Cross-process deduplication for inbound OneBot message events."""

import logging

from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import MessageEvent
from nonebot.exception import IgnoredException
from nonebot.message import event_preprocessor

from rumi_helper.plugins.chat_message_utils import event_dedup_key
from rumi_helper.services.runtime import get_runtime

logger = logging.getLogger(__name__)


@event_preprocessor
async def deduplicate_message_event(event: Event) -> None:
    """Drop replayed MessageEvents before any matcher can produce side effects."""
    if not isinstance(event, MessageEvent):
        return

    key = event_dedup_key(event)
    try:
        runtime = get_runtime()
    except RuntimeError:
        logger.error("消息去重时 Runtime 尚未就绪 key=%s", key)
        raise IgnoredException("runtime unavailable") from None

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
