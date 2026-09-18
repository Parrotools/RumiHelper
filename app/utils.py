"""通用小工具。"""

import asyncio
import time
from typing import Any

QQ_TEXT_SAFE_LIMIT = 4000


class ReplyGate:
    """进程内交互回复闸门，统一限制并发处理产生的瞬时发送。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._next_send_at = 0.0

    async def wait_for_slot(self, rate_limit_per_second: float) -> None:
        if rate_limit_per_second <= 0:
            return
        interval = 1.0 / rate_limit_per_second
        async with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_send_at - now)
            self._next_send_at = max(now, self._next_send_at) + interval
        if delay:
            await asyncio.sleep(delay)


def truncate_for_qq(text: str, limit: int = QQ_TEXT_SAFE_LIMIT) -> str:
    """超长回复截断，避免超出 QQ 单条消息上限导致发送失败。"""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n……（内容过长已截断，原文共 {len(text)} 字符）"


def local_reply_delay(runtime: Any, message: str) -> float:
    """按回复文本量计算不调用 LLM 的交互延迟。"""
    settings = runtime.settings
    try:
        legacy_max = getattr(settings, "non_llm_reply_delay_seconds", None)
        max_delay = float(
            legacy_max
            if legacy_max is not None
            else getattr(settings, "non_llm_reply_delay_max_seconds", 4.0)
        )
        if max_delay <= 0:
            return 0.0
        min_delay = max(0.0, min(float(getattr(settings, "non_llm_reply_delay_min_seconds", 0.5)), max_delay))
        chars_per_second = max(1.0, float(getattr(settings, "non_llm_reply_delay_chars_per_second", 35.0)))
    except (TypeError, ValueError):
        min_delay, max_delay, chars_per_second = 0.5, 4.0, 35.0

    visible_chars = len("".join(str(message).split()))
    return min(max_delay, max(min_delay, visible_chars / chars_per_second))


async def send_reply(matcher: Any, runtime: Any, message: str) -> None:
    """发送交互回复，并经过统一发送闸门。"""
    try:
        rate = float(getattr(runtime.settings, "interactive_send_rate_limit_per_second", 0.0))
    except (AttributeError, TypeError, ValueError):
        rate = 0.0
    gate = getattr(runtime, "reply_gate", None)
    if gate is None:
        gate = ReplyGate()
        try:
            runtime.reply_gate = gate
        except AttributeError:
            pass
    await gate.wait_for_slot(max(0.0, rate))
    await matcher.send(message)


async def send_local_reply(matcher: Any, runtime: Any, message: str) -> None:
    """发送不经过 LLM 的交互回复，并按文本量计算处理延迟。"""
    delay = local_reply_delay(runtime, message)
    if delay:
        await asyncio.sleep(delay)
    await send_reply(matcher, runtime, message)
