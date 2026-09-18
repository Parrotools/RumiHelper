"""Concrete implementations for the AgentHarness business tools."""

from typing import Any
from zoneinfo import ZoneInfo

from rumi_helper.services.github.tracker import format_check_result, parse_repo_url
from rumi_helper.services.health import format_status
from rumi_helper.services.qq.broadcast_parser import parse_targets
from rumi_helper.services.scheduler import SchedulerValidationError, parse_reminder_command
from rumi_helper.utils import truncate_for_qq


async def github_add(runtime: Any, user_id: str, arguments: dict[str, Any], session_key: str) -> str:
    del session_key
    repo = await runtime.github.add_repository(user_id, arguments["url"])
    return f'已添加仓库 {repo["repo_owner"]}/{repo["repo_name"]}。'


async def github_remove(runtime: Any, user_id: str, arguments: dict[str, Any], session_key: str) -> str:
    del session_key
    if not await runtime.github.remove_repository(user_id, arguments["url"]):
        from rumi_helper.services.github.tracker import GitHubTrackerError

        raise GitHubTrackerError("该仓库尚未添加")
    return "已移除仓库。"


async def github_list(runtime: Any, user_id: str, arguments: dict[str, Any], session_key: str) -> str:
    del arguments, session_key
    repos = await runtime.github.list_repositories(user_id)
    if not repos:
        return "你还没有添加 GitHub 仓库。"
    return "你的 GitHub 仓库：\n" + "\n".join(
        f'{repo["repo_owner"]}/{repo["repo_name"]}' for repo in repos
    )


async def github_check(runtime: Any, user_id: str, arguments: dict[str, Any], session_key: str) -> str:
    del session_key
    return format_check_result(await runtime.github.check(user_id, arguments["url"]))


async def github_info(runtime: Any, user_id: str, arguments: dict[str, Any], session_key: str) -> str:
    del user_id, session_key
    ref = parse_repo_url(arguments["url"])
    info = await runtime.github_client.get_repository(ref.owner, ref.name)
    return (
        f"{ref.owner}/{ref.name}\n"
        f"Star：{info.get('stargazers_count', 0)}  Fork：{info.get('forks_count', 0)}\n"
        f"开放 Issue/PR：{info.get('open_issues_count', 0)}\n{info.get('html_url', ref.url)}"
    )


async def github_watch(runtime: Any, user_id: str, arguments: dict[str, Any], session_key: str) -> str:
    del session_key
    await runtime.github.watch(user_id, arguments["url"], arguments["target"])
    return "GitHub 通知目标已添加。"


async def github_digest_set(runtime: Any, user_id: str, arguments: dict[str, Any], session_key: str) -> str:
    del user_id, session_key
    targets = parse_targets(arguments["targets"])
    await runtime.db.replace_github_digest_targets([target.to_dict() for target in targets])
    await runtime.scheduler.sync_system_task("github_digest", runtime.settings.github_digest_cron)
    return f"GitHub 定时汇总目标已更新：{arguments['targets']}。"


async def github_digest_list(runtime: Any, user_id: str, arguments: dict[str, Any], session_key: str) -> str:
    del user_id, arguments, session_key
    targets = await runtime.db.fetch_github_digest_targets()
    if not targets:
        return "尚未设置 GitHub 定时汇总目标。"
    return "GitHub 定时汇总目标：\n" + "\n".join(
        f'{target["target_type"]}:{target["target_id"]}' for target in targets
    )


async def github_digest_clear(runtime: Any, user_id: str, arguments: dict[str, Any], session_key: str) -> str:
    del user_id, arguments, session_key
    await runtime.db.replace_github_digest_targets([])
    await runtime.scheduler.sync_system_task("github_digest", "")
    return "GitHub 定时汇总目标已清空，定时发送已关闭。"


async def memory_save(runtime: Any, user_id: str, arguments: dict[str, Any], session_key: str) -> str:
    del session_key
    memory_id = await runtime.memory.save(user_id, arguments["memory_type"], arguments["content"])
    if memory_id is None:
        return "这条记忆的重要度太低，未保存。"
    return f"已保存长期记忆（{arguments['memory_type']}，编号 {memory_id}）。"


async def memory_list(runtime: Any, user_id: str, arguments: dict[str, Any], session_key: str) -> str:
    del arguments, session_key
    memories = await runtime.memory.list_for_context(user_id, limit=20)
    if not memories:
        return "暂时没有长期记忆。"
    return "你的长期记忆：\n" + "\n".join(
        f"{memory['id']}. [{memory['type']}] {memory['content']}" for memory in memories
    )


async def notification_set(runtime: Any, user_id: str, arguments: dict[str, Any], session_key: str) -> str:
    del session_key
    await runtime.notifications.set(user_id, **{arguments["field"]: arguments["enabled"]})
    label = {
        "reminder_notify": "提醒",
        "github_notify": "GitHub",
        "daily_report": "日报",
    }[arguments["field"]]
    return f"{label}通知已{'开启' if arguments['enabled'] else '关闭'}。"


async def reminder_create(runtime: Any, user_id: str, arguments: dict[str, Any], session_key: str) -> str:
    del session_key
    if not (await runtime.notifications.get(user_id))["reminder_notify"]:
        raise SchedulerValidationError("提醒通知默认关闭，请先开启提醒通知")
    timezone_info = ZoneInfo(runtime.settings.scheduler_timezone)
    run_at, cron_expression, _ = parse_reminder_command(
        f"/remind {arguments['timing']} -- {arguments['message']}", timezone_info
    )
    task_id = await runtime.scheduler.create_reminder(
        user_id,
        arguments["message"],
        run_at=run_at,
        cron_expression=cron_expression,
    )
    return f"提醒已创建（编号 {task_id}）。"


async def schedule_group_message(
    runtime: Any,
    user_id: str,
    arguments: dict[str, Any],
    session_key: str,
) -> str:
    del session_key
    timezone_info = ZoneInfo(runtime.settings.scheduler_timezone)
    run_at, cron_expression, _ = parse_reminder_command(
        f"/remind {arguments['timing']} -- {arguments['message']}", timezone_info
    )
    task_id = await runtime.scheduler.create_group_message(
        user_id,
        arguments["group_id"],
        arguments["message"],
        run_at=run_at,
        cron_expression=cron_expression,
    )
    return f"定时群发已创建（编号 {task_id}），目标群：{arguments['group_id']}。"


async def schedule_topic_joke(
    runtime: Any,
    user_id: str,
    arguments: dict[str, Any],
    session_key: str,
) -> str:
    del session_key
    timezone_info = ZoneInfo(runtime.settings.scheduler_timezone)
    run_at, cron_expression, _ = parse_reminder_command(
        f"/remind {arguments['timing']} -- {arguments['topic']}", timezone_info
    )
    task_id = await runtime.scheduler.create_topic_joke(
        user_id,
        arguments["group_id"],
        arguments["topic"],
        run_at=run_at,
        cron_expression=cron_expression,
    )
    return f"主题段子任务已创建（编号 {task_id}），目标群：{arguments['group_id']}。"


async def broadcast_message(runtime: Any, user_id: str, arguments: dict[str, Any], session_key: str) -> str:
    del user_id, session_key
    targets = parse_targets(arguments["targets"])
    report = await runtime.dispatcher.broadcast(targets, arguments["message"])
    return report.summary_text()


async def clear_session(runtime: Any, user_id: str, arguments: dict[str, Any], session_key: str) -> str:
    del user_id, arguments
    await runtime.sessions.clear(session_key)
    return "当前会话已清空。"


async def status(runtime: Any, user_id: str, arguments: dict[str, Any], session_key: str) -> str:
    del arguments, session_key
    health = await runtime.health.check()
    return format_status(
        health,
        detailed=runtime.permission.is_admin(user_id),
    )


async def daily_report(runtime: Any, user_id: str, arguments: dict[str, Any], session_key: str) -> str:
    del arguments, session_key
    return truncate_for_qq(await runtime.report.build_daily_report(user_id))
