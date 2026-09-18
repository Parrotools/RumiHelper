"""提醒与通知设置命令。"""

from zoneinfo import ZoneInfo

from nonebot import on_message
from nonebot.adapters.onebot.v11 import MessageEvent
from nonebot.rule import Rule

from rumi_helper.plugins.chat_message_utils import (
    claim_event,
    command_is_addressed,
    strip_bot_mention,
)
from rumi_helper.services.runtime import get_runtime
from rumi_helper.services.scheduler import (
    SchedulerValidationError,
    parse_group_schedule_command,
    parse_joke_schedule_command,
    parse_reminder_command,
)
from rumi_helper.utils import send_local_reply

_REMIND = "/remind"
_NOTIFY = "/notify"
_SCHEDULE = "/schedule"
_NOTIFICATION_NAMES = {
    "reminder": "reminder_notify",
    "github": "github_notify",
    "report": "daily_report",
    "daily_report": "daily_report",
}


def parse_notification_command(text: str) -> tuple[str, bool]:
    parts = text.strip().split()
    if len(parts) != 3 or parts[0].lower() != _NOTIFY:
        raise SchedulerValidationError("格式：/notify reminder|github|report on|off")
    name, enabled = parts[1].lower(), parts[2].lower()
    if name not in _NOTIFICATION_NAMES or enabled not in ("on", "off"):
        raise SchedulerValidationError("支持：/notify reminder|github|report on|off")
    return _NOTIFICATION_NAMES[name], enabled == "on"


async def _remind_rule(event: MessageEvent) -> bool:
    if str(event.user_id) == str(event.self_id):
        return False
    if not command_is_addressed(event):
        return False
    text = strip_bot_mention(event.message.extract_plain_text()).lower()
    if text == _REMIND or text.startswith(f"{_REMIND} "):
        return claim_event(event)
    return False


async def _notify_rule(event: MessageEvent) -> bool:
    if str(event.user_id) == str(event.self_id):
        return False
    if not command_is_addressed(event):
        return False
    if strip_bot_mention(event.message.extract_plain_text()).lower().startswith(f"{_NOTIFY} "):
        return claim_event(event)
    return False


async def _schedule_rule(event: MessageEvent) -> bool:
    if str(event.user_id) == str(event.self_id):
        return False
    if not command_is_addressed(event):
        return False
    text = strip_bot_mention(event.message.extract_plain_text()).lower()
    if text == _SCHEDULE or text.startswith(f"{_SCHEDULE} "):
        return claim_event(event)
    return False


remind_matcher = on_message(rule=Rule(_remind_rule), priority=7, block=True)
notify_matcher = on_message(rule=Rule(_notify_rule), priority=7, block=True)
schedule_matcher = on_message(rule=Rule(_schedule_rule), priority=7, block=True)


@remind_matcher.handle()
async def _handle_remind(event: MessageEvent):
    runtime = get_runtime()
    try:
        timezone_info = ZoneInfo(runtime.settings.scheduler_timezone)
        run_at, cron_expression, message = parse_reminder_command(
            strip_bot_mention(event.message.extract_plain_text()), timezone_info
        )
        settings = await runtime.notifications.get(str(event.user_id))
        if not settings["reminder_notify"]:
            await send_local_reply(remind_matcher, runtime, "提醒通知默认关闭，请先发送 /notify reminder on。")
            return
        task_id = await runtime.scheduler.create_reminder(
            str(event.user_id), message, run_at=run_at, cron_expression=cron_expression
        )
    except (SchedulerValidationError, ValueError) as e:
        await send_local_reply(remind_matcher, runtime, f"格式错误：{e}")
        return
    await send_local_reply(remind_matcher, runtime, f"提醒已创建（编号 {task_id}）。")


@notify_matcher.handle()
async def _handle_notify(event: MessageEvent):
    runtime = get_runtime()
    try:
        field, enabled = parse_notification_command(strip_bot_mention(event.message.extract_plain_text()))
        await runtime.notifications.set(str(event.user_id), **{field: enabled})
    except SchedulerValidationError as e:
        await send_local_reply(notify_matcher, runtime, f"格式错误：{e}")
        return
    labels = {"reminder_notify": "提醒", "github_notify": "GitHub", "daily_report": "日报"}
    await send_local_reply(notify_matcher, runtime, f"{labels[field]}通知已{'开启' if enabled else '关闭'}。")


@schedule_matcher.handle()
async def _handle_schedule(event: MessageEvent):
    runtime = get_runtime()
    if not runtime.permission.is_admin(str(event.user_id)):
        await send_local_reply(schedule_matcher, runtime, "该命令仅管理员可用。")
        return
    try:
        timezone_info = ZoneInfo(runtime.settings.scheduler_timezone)
        command = strip_bot_mention(event.message.extract_plain_text())
        if command.lower().startswith(f"{_SCHEDULE} joke"):
            group_id, run_at, cron_expression, topic = parse_joke_schedule_command(command, timezone_info)
            task_id = await runtime.scheduler.create_topic_joke(
                str(event.user_id), group_id, topic, run_at=run_at, cron_expression=cron_expression
            )
            task_label = "主题段子任务"
        else:
            group_id, run_at, cron_expression, message = parse_group_schedule_command(command, timezone_info)
            task_id = await runtime.scheduler.create_group_message(
                str(event.user_id), group_id, message, run_at=run_at, cron_expression=cron_expression
            )
            task_label = "定时群发"
    except (SchedulerValidationError, ValueError) as e:
        await send_local_reply(schedule_matcher, runtime, f"格式错误：{e}")
        return
    schedule_label = cron_expression or run_at.strftime("%Y-%m-%d %H:%M")
    await send_local_reply(
        schedule_matcher,
        runtime,
        f"{task_label}已创建（编号 {task_id}），目标群：{group_id}，时间：{schedule_label}。",
    )
