"""Pure parsing and normalization helpers for scheduler commands."""

from datetime import UTC, datetime, tzinfo

from apscheduler.triggers.cron import CronTrigger

from rumi_helper.utils import QQ_TEXT_SAFE_LIMIT


class SchedulerValidationError(ValueError):
    """任务或提醒格式不合法。"""


def parse_reminder_command(
    text: str,
    timezone_info: tzinfo = UTC,
) -> tuple[datetime | None, str | None, str]:
    body = text.strip()
    if body.lower().startswith("/remind"):
        body = body[len("/remind"):].strip()
    schedule, separator, message = body.partition("--")
    message = message.strip()
    schedule = schedule.strip()
    if not separator or not schedule or not message:
        raise SchedulerValidationError(
            "格式：/remind YYYY-MM-DD HH:MM -- 内容，或 "
            "/remind cron:0 8 * * * -- 内容"
        )
    if len(message) > QQ_TEXT_SAFE_LIMIT:
        raise SchedulerValidationError(
            f"内容不能超过 {QQ_TEXT_SAFE_LIMIT} 个字符"
        )

    if schedule.lower().startswith("cron:"):
        cron_expression = schedule[5:].strip()
        try:
            CronTrigger.from_crontab(
                cron_expression,
                timezone=timezone_info,
            )
        except ValueError as exc:
            raise SchedulerValidationError(
                "cron 表达式无效，应为 5 段格式"
            ) from exc
        return None, cron_expression, message

    try:
        run_at = datetime.strptime(
            schedule,
            "%Y-%m-%d %H:%M",
        ).replace(tzinfo=timezone_info)
    except ValueError as exc:
        raise SchedulerValidationError(
            "时间格式应为 YYYY-MM-DD HH:MM"
        ) from exc
    if run_at <= datetime.now(timezone_info):
        raise SchedulerValidationError("提醒时间必须晚于当前时间")
    return run_at, None, message


def parse_group_schedule_command(
    text: str,
    timezone_info: tzinfo = UTC,
) -> tuple[str, datetime | None, str | None, str]:
    """解析定时群发命令，返回 (群号, 一次性时间, cron, 消息)。"""
    body = text.strip()
    if body.lower().startswith("/schedule"):
        body = body[len("/schedule"):].strip()
    schedule, separator, message = body.partition("--")
    schedule = schedule.strip()
    message = message.strip()
    if not separator or not schedule or not message:
        raise SchedulerValidationError(
            "格式：/schedule group:群号 YYYY-MM-DD HH:MM -- 内容，或 "
            "/schedule group:群号 cron:0 8 * * * -- 内容"
        )

    target, separator, timing = schedule.partition(" ")
    if not separator or not timing.strip():
        raise SchedulerValidationError(
            "格式：/schedule group:群号 YYYY-MM-DD HH:MM -- 内容，或 "
            "/schedule group:群号 cron:0 8 * * * -- 内容"
        )
    target_type, target_separator, target_id = target.partition(":")
    target_type = target_type.strip().lower()
    target_id = target_id.strip()
    if (
        target_type != "group"
        or not target_separator
        or not target_id.isdigit()
    ):
        raise SchedulerValidationError("目标必须是 group:群号")

    run_at, cron_expression, _ = parse_reminder_command(
        f"/remind {timing.strip()} -- {message}",
        timezone_info,
    )
    return target_id, run_at, cron_expression, message


def parse_joke_schedule_command(
    text: str,
    timezone_info: tzinfo = UTC,
) -> tuple[str, datetime | None, str | None, str]:
    """解析主题段子定时命令，返回 (群号, 一次性时间, cron, 主题)。"""
    body = text.strip()
    if body.lower().startswith("/schedule"):
        body = body[len("/schedule"):].strip()
    if not body.lower().startswith("joke "):
        raise SchedulerValidationError(
            "格式：/schedule joke group:群号 YYYY-MM-DD HH:MM -- 主题，或 "
            "/schedule joke group:群号 cron:0 12 * * * -- 主题"
        )
    return parse_group_schedule_command(
        f"/schedule {body[5:].strip()}",
        timezone_info,
    )


def normalize_generated_content(content: str) -> str:
    return " ".join(content.casefold().split())
