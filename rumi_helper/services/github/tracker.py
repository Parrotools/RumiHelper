"""GitHub 仓库登记、快照和变化检测。"""

import asyncio
import logging
import re
import weakref
from datetime import UTC, datetime, tzinfo
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from rumi_helper.database.db import Database
from rumi_helper.services.github.models import GitHubRepoRef, GitHubSnapshot
from rumi_helper.services.notifications import NotificationSettingsService
from rumi_helper.utils import truncate_for_qq

logger = logging.getLogger(__name__)

_GITHUB_HOSTS = {"github.com", "www.github.com"}
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_MARKDOWN_LINK_PATTERN = re.compile(r"^\[[^\]]+\]\((https://[^)]+)\)$")


class GitHubTrackerError(ValueError):
    """GitHub 仓库或监控操作不合法。"""


def parse_repo_url(url: str) -> GitHubRepoRef:
    value = str(url).strip()
    markdown_link = _MARKDOWN_LINK_PATTERN.fullmatch(value)
    if markdown_link:
        value = markdown_link.group(1)
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname not in _GITHUB_HOSTS:
        raise GitHubTrackerError("只支持 https://github.com/owner/repo 格式")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2:
        raise GitHubTrackerError("GitHub 仓库 URL 必须包含 owner 和 repo")
    owner, name = parts[0], parts[1].removesuffix(".git")
    if not _NAME_PATTERN.fullmatch(owner) or not _NAME_PATTERN.fullmatch(name):
        raise GitHubTrackerError("GitHub 仓库名称无效")
    return GitHubRepoRef(owner, name, f"https://github.com/{owner}/{name}")


def compare_snapshots(previous: dict | None, current: dict | GitHubSnapshot) -> list[dict]:
    if previous is None:
        return []
    current_data = current.to_dict() if isinstance(current, GitHubSnapshot) else current
    changes: list[dict] = []
    if previous["latest_commit_sha"] != current_data["latest_commit_sha"]:
        changes.append(
            {
                "type": "commit",
                "sha": current_data["latest_commit_sha"],
                "message": current_data["latest_commit_message"],
                "author": current_data["latest_commit_author"],
                "time": current_data["latest_commit_time"],
            }
        )
    for field, change_type in (("stars", "stars"), ("forks", "forks"), ("open_issues_count", "issues")):
        changed = (
            current_data[field] != previous[field]
            if field == "open_issues_count"
            else current_data[field] > previous[field]
        )
        if changed:
            changes.append(
                {"type": change_type, "from": previous[field], "to": current_data[field], "delta": current_data[field] - previous[field]}
            )
    if current_data["latest_release"] != previous["latest_release"] and current_data["latest_release"]:
        changes.append({"type": "release", "from": previous["latest_release"], "to": current_data["latest_release"]})
    return changes


class GitHubTracker:
    def __init__(
        self,
        db: Database,
        client,
        dispatcher=None,
        notifications=None,
        timezone_name: str = "Asia/Shanghai",
        outbound_max_attempts: int = 3,
    ):
        self._db = db
        self._client = client
        self._dispatcher = dispatcher
        self._notifications = notifications or NotificationSettingsService(db)
        self._timezone = ZoneInfo(timezone_name)
        self._outbound_max_attempts = max(1, int(outbound_max_attempts))
        self._repo_check_locks: weakref.WeakValueDictionary[int, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    async def add_repository(self, owner_id: str, url: str) -> dict:
        owner_id = str(owner_id).strip()
        if not owner_id:
            raise GitHubTrackerError("owner_id 不能为空")
        ref = parse_repo_url(url)
        repo_id = await self._db.insert_github_repository(owner_id, ref.owner, ref.name, ref.url)
        row = await self._db.fetch_github_repository(str(owner_id), ref.owner, ref.name)
        return {**row, "id": repo_id}

    async def remove_repository(self, owner_id: str, url: str) -> bool:
        ref = parse_repo_url(url)
        return await self._db.delete_github_repository(str(owner_id), ref.owner, ref.name)

    async def list_repositories(self, owner_id: str) -> list[dict]:
        return await self._db.fetch_github_repositories(str(owner_id))

    async def check(self, owner_id: str, url: str) -> dict:
        repo = await self._get_owned_repository(owner_id, url)
        return await self._check_row(repo)

    async def run_scheduled_check(self, task: dict) -> None:
        del task
        if self._dispatcher is None:
            raise GitHubTrackerError("GitHub 通知未配置消息发送器")
        for repo in await self._db.fetch_github_repositories():
            try:
                await self._check_row(repo, queue_notifications=True)
            except Exception:
                logger.exception("GitHub 检查失败 repo_id=%s", repo["id"])

    async def run_scheduled_digest(self, task: dict) -> None:
        """定时汇总所有仓库的最新 commit，并把同一份消息私发给配置收件人。"""
        del task
        if self._dispatcher is None:
            raise GitHubTrackerError("GitHub 汇总未配置消息发送器")
        targets = await self._db.fetch_github_digest_targets()
        if not targets:
            logger.warning("GitHub 定时汇总未配置收件人，跳过发送")
            return

        items: list[dict] = []
        failures: list[dict] = []
        seen_repositories: set[tuple[str, str]] = set()
        for repo in await self._db.fetch_github_repositories():
            repository_key = (str(repo["repo_owner"]).lower(), str(repo["repo_name"]).lower())
            if repository_key in seen_repositories:
                continue
            seen_repositories.add(repository_key)
            try:
                snapshot = await self._client.fetch_snapshot(
                    GitHubRepoRef(repo["repo_owner"], repo["repo_name"], repo["repo_url"])
                )
            except Exception:
                logger.exception("GitHub 汇总获取失败 repo_id=%s", repo["id"])
                failures.append(repo)
                continue
            items.append({"repo": repo, "snapshot": snapshot})

        message = format_github_digest(
            items,
            failures=failures,
            generated_at=datetime.now(self._timezone),
            timezone_info=self._timezone,
        )
        for target in targets:
            sender = self._dispatcher.enqueue_user if target["target_type"] == "user" else self._dispatcher.enqueue_group
            try:
                await sender(target["target_id"], message)
            except Exception:
                logger.exception(
                    "GitHub 汇总发送失败 target=%s:%s", target["target_type"], target["target_id"]
                )

    async def _check_row(self, repo: dict, *, queue_notifications: bool = False) -> dict:
        repo_id = int(repo["id"])
        lock = self._repo_check_locks.setdefault(repo_id, asyncio.Lock())
        async with lock:
            snapshot = await self._client.fetch_snapshot(
                GitHubRepoRef(repo["repo_owner"], repo["repo_name"], repo["repo_url"])
            )
            previous = await self._db.fetch_latest_github_snapshot(repo_id)
            changes = compare_snapshots(previous, snapshot)
            result = {"repo": repo, "snapshot": snapshot, "changes": changes}

            deliveries: list[dict] = []
            if queue_notifications and changes:
                settings = await self._notifications.get(repo["owner_id"])
                if settings["github_notify"]:
                    message = format_check_result(result)
                    deliveries = [
                        {
                            "target_type": target["target_type"],
                            "target_id": target["target_id"],
                            "content": message,
                        }
                        for target in await self._db.fetch_github_notifications(repo_id)
                    ]

            if deliveries:
                await self._db.insert_github_snapshot_and_outbound(
                    repo_id,
                    snapshot.to_dict(),
                    deliveries,
                    self._outbound_max_attempts,
                )
                if self._dispatcher is not None:
                    self._dispatcher.notify_queue_changed()
            else:
                await self._db.insert_github_snapshot(repo_id, snapshot.to_dict())
            return result

    async def watch(self, owner_id: str, url: str, target: str) -> None:
        repo = await self._get_owned_repository(owner_id, url)
        target_type, separator, target_id = target.partition(":")
        if not separator or target_type not in ("user", "group") or not target_id.isdigit():
            raise GitHubTrackerError("通知目标应为 user:QQ号 或 group:群号")
        await self._db.upsert_github_notification(repo["id"], target_type, target_id)

    async def _get_owned_repository(self, owner_id: str, url: str) -> dict:
        ref = parse_repo_url(url)
        repo = await self._db.fetch_github_repository(str(owner_id), ref.owner, ref.name)
        if repo is None:
            raise GitHubTrackerError("该仓库尚未添加")
        return repo


def format_check_result(result: dict) -> str:
    repo = result["repo"]
    name = f'{repo["repo_owner"]}/{repo["repo_name"]}'
    changes = result["changes"]
    if not changes:
        return f"已检查 {name}：暂无新变化。"
    lines = [f"{name} 有新变化："]
    for change in changes:
        if change["type"] == "commit":
            lines.append(f'提交：{change["message"]}（{change["author"]}）')
        elif change["type"] == "release":
            lines.append(f'发布版本：{change["to"]}')
        else:
            labels = {"stars": "Star", "forks": "Fork", "issues": "开放 Issue/PR"}
            lines.append(f'{labels[change["type"]]}：{change["from"]} → {change["to"]}')
    return "\n".join(lines)


def format_github_digest(
    items: list[dict],
    *,
    failures: list[dict] | None = None,
    generated_at: datetime | None = None,
    timezone_info: tzinfo | None = None,
) -> str:
    """格式化定时汇总，包含每个仓库最新 commit 的时间、作者和消息。"""
    timezone_info = timezone_info or ZoneInfo("Asia/Shanghai")
    generated_at = generated_at or datetime.now(timezone_info)
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=timezone_info)
    generated_at = generated_at.astimezone(timezone_info)

    lines = [
        f"【GitHub 仓库汇总 {generated_at.strftime('%Y-%m-%d %H:%M')}】",
        "",
    ]
    if not items:
        lines.append("暂无已登记的 GitHub 仓库。")
    for index, item in enumerate(items, 1):
        repo = item["repo"]
        snapshot: GitHubSnapshot = item["snapshot"]
        name = f'{repo["repo_owner"]}/{repo["repo_name"]}'
        lines.append(f"{index}. {name}")
        if not snapshot.latest_commit_sha:
            lines.append("Last commit：暂无提交")
            lines.append("")
            continue
        lines.append(f"Last commit 时间：{_format_commit_time(snapshot.latest_commit_time, timezone_info)}")
        lines.append(f"Last commit 作者：{snapshot.latest_commit_author or 'unknown'}")
        lines.append("Last commit 消息：")
        commit_message = snapshot.latest_commit_message.strip() or "（无提交消息）"
        lines.extend(f"  {line}" for line in commit_message.splitlines())
        lines.append("")

    if failures:
        lines.append("获取失败：")
        lines.extend(f'- {repo["repo_owner"]}/{repo["repo_name"]}' for repo in failures)

    return truncate_for_qq("\n".join(lines).rstrip())


def _format_commit_time(value: str, timezone_info: tzinfo) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "未知"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return raw
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(timezone_info).strftime("%Y-%m-%d %H:%M:%S")
