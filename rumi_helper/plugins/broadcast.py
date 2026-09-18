"""broadcast 插件：管理员多目标群发 + 确认机制 + 限速 + 逐目标结果记录。

流程：
    /broadcast user:123,group:456 -- 消息
    -> （默认）返回预览，等待该管理员 /confirm（TTL 内）或 /cancel
    -> 确认后逐目标发送，最后汇报成功/失败统计
"""

import math

from nonebot import logger, on_message
from nonebot.adapters.onebot.v11 import MessageEvent
from nonebot.rule import Rule

from rumi_helper.plugins.chat_message_utils import (
    claim_event,
    command_is_addressed,
    strip_bot_mention,
)
from rumi_helper.services.qq.broadcast_parser import BroadcastFormatError, parse_broadcast_command
from rumi_helper.services.runtime import get_runtime
from rumi_helper.utils import send_local_reply

_BROADCAST = "/broadcast"
_CONFIRM = "/confirm"
_CANCEL = "/cancel"


async def _broadcast_rule(event: MessageEvent) -> bool:
    if str(event.user_id) == str(event.self_id):
        return False
    if not command_is_addressed(event):
        return False
    text = strip_bot_mention(event.message.extract_plain_text())
    if text.lower().startswith(_BROADCAST):
        return claim_event(event)
    return False


async def _confirm_rule(event: MessageEvent) -> bool:
    if str(event.user_id) == str(event.self_id):
        return False
    if command_is_addressed(event) and strip_bot_mention(event.message.extract_plain_text()) == _CONFIRM:
        return claim_event(event)
    return False


async def _cancel_rule(event: MessageEvent) -> bool:
    if str(event.user_id) == str(event.self_id):
        return False
    if command_is_addressed(event) and strip_bot_mention(event.message.extract_plain_text()) == _CANCEL:
        return claim_event(event)
    return False


broadcast_matcher = on_message(rule=Rule(_broadcast_rule), priority=5, block=True)
confirm_matcher = on_message(rule=Rule(_confirm_rule), priority=5, block=True)
cancel_matcher = on_message(rule=Rule(_cancel_rule), priority=5, block=True)


@broadcast_matcher.handle()
async def _handle_broadcast(event: MessageEvent):
    runtime = get_runtime()
    sender_id = str(event.user_id)

    if not runtime.permission.is_admin(sender_id):
        await send_local_reply(broadcast_matcher, runtime, "该命令仅管理员可用。")
        return

    text = strip_bot_mention(event.message.extract_plain_text())
    try:
        targets, message = parse_broadcast_command(text)
    except BroadcastFormatError as e:
        await send_local_reply(broadcast_matcher, runtime, f"格式错误：{e}")
        return

    if len(targets) > runtime.settings.max_broadcast_recipients:
        await send_local_reply(
            broadcast_matcher,
            runtime,
            f"目标数量 {len(targets)} 超过上限 {runtime.settings.max_broadcast_recipients}，已拒绝。"
        )
        return

    target_lines = "\n".join(f"- {t.display()}" for t in targets)
    minutes = max(1, math.ceil(runtime.settings.broadcast_confirm_ttl_seconds / 60))
    preview = (
        f"准备发送：\n目标：\n{target_lines}\n\n"
        f"消息：\n{message}\n\n总计 {len(targets)} 个目标。\n"
        f"请在 {minutes} 分钟内输入 /confirm 继续，或 /cancel 取消。"
    )

    if runtime.settings.broadcast_require_confirm:
        pending_id = await runtime.db.create_pending_broadcast(
            admin_id=sender_id,
            targets=[t.to_dict() for t in targets],
            content=message,
            ttl_seconds=runtime.settings.broadcast_confirm_ttl_seconds,
        )
        logger.info("broadcast 预览创建 pending_id=%s admin=%s targets=%d", pending_id, sender_id, len(targets))
        await send_local_reply(broadcast_matcher, runtime, preview)
        return

    report = await runtime.dispatcher.broadcast(targets, message)
    await send_local_reply(broadcast_matcher, runtime, report.summary_text())


@confirm_matcher.handle()
async def _handle_confirm(event: MessageEvent):
    runtime = get_runtime()
    sender_id = str(event.user_id)

    if not runtime.permission.is_admin(sender_id):
        await send_local_reply(confirm_matcher, runtime, "该命令仅管理员可用。")
        return

    pending = await runtime.db.get_active_pending(sender_id)
    if pending is None:
        await send_local_reply(confirm_matcher, runtime, "没有待确认的群发任务（可能已过期或不存在）。")
        return

    # 原子领取：只有 active -> executing 成功的一方才能实际发送。
    # 一旦进入 executing 就不再允许 /cancel，避免“已开始发送却又显示取消”的竞态。
    if not await runtime.db.claim_pending(pending["id"]):
        await send_local_reply(confirm_matcher, runtime, "该任务已被处理。")
        return

    from rumi_helper.services.qq.broadcast_parser import parse_targets

    try:
        targets = parse_targets(",".join(f"{t['type']}:{t['id']}" for t in pending["targets"]))
    except BroadcastFormatError:
        await runtime.db.finish_pending(pending["id"], "failed")
        logger.error("pending_broadcast %s 目标数据损坏", pending["id"])
        await send_local_reply(confirm_matcher, runtime, "任务数据异常，已放弃发送。")
        return

    logger.info("broadcast 执行 pending_id=%s admin=%s targets=%d", pending["id"], sender_id, len(targets))
    try:
        report = await runtime.dispatcher.broadcast(targets, pending["content"])
    except Exception:  # noqa: BLE001 - execution failure must transition the durable state
        await runtime.db.finish_pending(pending["id"], "failed")
        logger.exception("broadcast 执行失败 pending_id=%s admin=%s", pending["id"], sender_id)
        await send_local_reply(confirm_matcher, runtime, "群发执行失败，任务已标记为失败。")
        return

    if not await runtime.db.finish_pending(pending["id"], "confirmed"):
        logger.error("broadcast 终态写入失败 pending_id=%s admin=%s", pending["id"], sender_id)
    await send_local_reply(confirm_matcher, runtime, report.summary_text())


@cancel_matcher.handle()
async def _handle_cancel(event: MessageEvent):
    runtime = get_runtime()
    sender_id = str(event.user_id)

    if not runtime.permission.is_admin(sender_id):
        await send_local_reply(cancel_matcher, runtime, "该命令仅管理员可用。")
        return

    pending = await runtime.db.get_active_pending(sender_id)
    if pending is None:
        await send_local_reply(cancel_matcher, runtime, "没有待取消的群发任务。")
        return
    if not await runtime.db.finish_pending(pending["id"], "cancelled"):
        await send_local_reply(cancel_matcher, runtime, "该任务已被处理。")
        return
    logger.info("broadcast 取消 pending_id=%s admin=%s", pending["id"], sender_id)
    await send_local_reply(cancel_matcher, runtime, "已取消。")
