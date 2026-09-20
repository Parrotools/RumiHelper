"""GitHub 仓库登记、手动检查和通知目标管理。"""

from nonebot import on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent
from nonebot.rule import Rule

from rumi_helper.plugins.chat_message_utils import (
    claim_event,
    command_is_addressed,
    strip_bot_mention,
)
from rumi_helper.security.identifiers import is_ascii_decimal_id
from rumi_helper.security.permissions import notification_target_requires_admin
from rumi_helper.services.github.client import GitHubAPIError
from rumi_helper.services.github.tracker import GitHubTrackerError, format_check_result, parse_repo_url
from rumi_helper.services.qq.broadcast_parser import BroadcastFormatError, parse_targets
from rumi_helper.services.runtime import get_runtime
from rumi_helper.utils import send_local_reply

_GITHUB = "/github"
_HELP = (
    "用法：\n"
    "/github add <URL>\n"
    "/github remove <URL>\n"
    "/github list\n"
    "/github check <URL>\n"
    "/github info <URL>\n"
    "/github watch <URL> user:QQ号|group:群号\n"
    "/github digest set user:QQ号1,user:QQ号2,group:群号\n"
    "/github digest list|clear"
)


def parse_github_command(text: str) -> tuple[str, list[str]]:
    parts = text.strip().split()
    if len(parts) < 2 or parts[0].lower() != _GITHUB:
        raise GitHubTrackerError(_HELP)
    return parts[1].lower(), parts[2:]


def parse_github_digest_command(
    args: list[str],
    *,
    max_targets: int | None = None,
) -> tuple[str, list[dict]]:
    """解析定时汇总目标命令；set 省略时也允许直接跟目标列表。"""
    if not args:
        raise GitHubTrackerError(_HELP)
    action = args[0].lower()
    if action in {"list", "clear"}:
        if len(args) != 1:
            raise GitHubTrackerError(_HELP)
        return action, []
    raw_targets = ",".join(args[1:] if action == "set" else args)
    try:
        targets = parse_targets(raw_targets)
    except BroadcastFormatError as e:
        raise GitHubTrackerError(str(e)) from e
    if max_targets is not None and len(targets) > max(0, int(max_targets)):
        raise GitHubTrackerError(
            f"目标数量 {len(targets)} 超过上限 {max_targets}"
        )
    return "set", [{"target_type": target.type, "target_id": target.id} for target in targets]


async def _github_rule(event: MessageEvent) -> bool:
    if str(event.user_id) == str(event.self_id):
        return False
    if not command_is_addressed(event):
        return False
    text = strip_bot_mention(event.message.extract_plain_text())
    if text.lower() == _GITHUB or text.lower().startswith(f"{_GITHUB} "):
        return claim_event(event)
    return False


github_matcher = on_message(rule=Rule(_github_rule), priority=6, block=True)


@github_matcher.handle()
async def _handle_github(event: MessageEvent):
    runtime = get_runtime()
    try:
        command, args = parse_github_command(strip_bot_mention(event.message.extract_plain_text()))
        if isinstance(event, GroupMessageEvent) and command in {"list", "digest"}:
            await send_local_reply(
                github_matcher,
                runtime,
                "为了隐私，GitHub 监控配置只能在私聊中查看或修改。",
            )
            return
        if command == "add":
            if len(args) != 1:
                raise GitHubTrackerError(_HELP)
            repo = await runtime.github.add_repository(str(event.user_id), args[0])
            await send_local_reply(github_matcher, runtime, f'已添加仓库 {repo["repo_owner"]}/{repo["repo_name"]}。')
        elif command == "remove":
            if len(args) != 1:
                raise GitHubTrackerError(_HELP)
            if not await runtime.github.remove_repository(str(event.user_id), args[0]):
                raise GitHubTrackerError("该仓库尚未添加")
            await send_local_reply(github_matcher, runtime, "已移除仓库。")
        elif command == "list":
            if args:
                raise GitHubTrackerError(_HELP)
            repos = await runtime.github.list_repositories(str(event.user_id))
            if not repos:
                await send_local_reply(github_matcher, runtime, "你还没有添加 GitHub 仓库。")
                return
            await send_local_reply(github_matcher, runtime, "\n".join(f'{r["repo_owner"]}/{r["repo_name"]}' for r in repos))
        elif command == "check":
            if len(args) != 1:
                raise GitHubTrackerError(_HELP)
            await send_local_reply(
                github_matcher, runtime, format_check_result(await runtime.github.check(str(event.user_id), args[0]))
            )
        elif command == "info":
            if len(args) != 1:
                raise GitHubTrackerError(_HELP)
            ref = parse_repo_url(args[0])
            info = await runtime.github_client.get_repository(ref.owner, ref.name)
            await send_local_reply(
                github_matcher,
                runtime,
                f'{ref.owner}/{ref.name}\n'
                f'Star：{info.get("stargazers_count", 0)}  Fork：{info.get("forks_count", 0)}\n'
                f'开放 Issue/PR：{info.get("open_issues_count", 0)}\n{ref.url}'
            )
        elif command == "watch":
            if len(args) != 2:
                raise GitHubTrackerError(_HELP)
            sender_id = str(event.user_id)
            target_type, separator, target_id = args[1].partition(":")
            if (
                not separator
                or target_type not in {"user", "group"}
                or not is_ascii_decimal_id(target_id)
            ):
                raise GitHubTrackerError("通知目标应为 user:QQ号 或 group:群号")
            if (
                notification_target_requires_admin(sender_id, target_type, target_id)
                and not runtime.permission.is_admin(sender_id)
            ):
                await send_local_reply(
                    github_matcher,
                    runtime,
                    "普通用户只能把 GitHub 通知发送给自己的私聊；其他用户或群聊目标仅管理员可配置。",
                )
                return
            await runtime.github.watch(sender_id, args[0], args[1])
            await send_local_reply(github_matcher, runtime, "GitHub 通知目标已添加。")
        elif command == "digest":
            if not runtime.permission.is_admin(str(event.user_id)):
                await send_local_reply(github_matcher, runtime, "该命令仅管理员可用。")
                return
            action, targets = parse_github_digest_command(
                args,
                max_targets=runtime.settings.max_broadcast_recipients,
            )
            if action == "list":
                configured = await runtime.db.fetch_github_digest_targets()
                if not configured:
                    await send_local_reply(github_matcher, runtime, "尚未设置 GitHub 定时汇总目标。")
                    return
                content = "\n".join(f'{target["target_type"]}:{target["target_id"]}' for target in configured)
                await send_local_reply(github_matcher, runtime, f"GitHub 定时汇总目标：\n{content}")
                return
            if action == "clear":
                await runtime.github.configure_digest_targets(
                    [],
                    runtime.scheduler,
                    "",
                )
                await send_local_reply(github_matcher, runtime, "GitHub 定时汇总目标已清空，定时发送已关闭。")
                return

            await runtime.github.configure_digest_targets(
                targets,
                runtime.scheduler,
                runtime.settings.github_digest_cron,
            )
            content = "\n".join(f'{target["target_type"]}:{target["target_id"]}' for target in targets)
            await send_local_reply(
                github_matcher,
                runtime,
                f"GitHub 定时汇总目标已更新：\n{content}\n"
                f"发送时间：{runtime.settings.scheduler_timezone}，cron={runtime.settings.github_digest_cron}",
            )
        else:
            await send_local_reply(github_matcher, runtime, _HELP)
    except (GitHubTrackerError, GitHubAPIError) as e:
        await send_local_reply(github_matcher, runtime, f"GitHub 操作失败：{e}")
