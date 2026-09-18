"""日报聚合与定时发送。"""

import json
import logging
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.database.db import Database
from app.services.github.tracker import compare_snapshots
from app.services.notifications import NotificationSettingsService
from app.services.qq.dispatcher import MessageDispatcher
from app.utils import truncate_for_qq


class ReportError(ValueError):
    """日报参数或运行配置不合法。"""


class ReportService:
    def __init__(
        self,
        db: Database,
        dispatcher: MessageDispatcher | None,
        notifications: NotificationSettingsService,
        timezone_name: str = "Asia/Shanghai",
        outbound_max_attempts: int = 3,
    ):
        try:
            self._timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as e:
            raise ReportError(f"时区不存在：{timezone_name}") from e
        self._db = db
        self._dispatcher = dispatcher
        self._notifications = notifications
        self._outbound_max_attempts = max(1, int(outbound_max_attempts))

    async def build_daily_report(self, owner_id: str, report_date: date | None = None) -> str:
        owner_id = str(owner_id).strip()
        if not owner_id:
            raise ReportError("owner_id 不能为空")
        report_date = report_date or datetime.now(self._timezone).date()
        period_start, period_end = self._period(report_date)
        github_items = await self._github_items(owner_id, period_start, period_end)
        tasks = await self._db.fetch_scheduled_tasks_for_report(owner_id, period_start, period_end)
        memories = await self._db.fetch_memories_for_report(owner_id, period_start, period_end)
        return format_daily_report(report_date, github_items, tasks, memories)

    async def run_scheduled_report(self, task: dict) -> None:
        if self._dispatcher is None:
            raise ReportError("日报未配置消息发送器")
        report_date = self._task_date(task)
        period_start, period_end = self._period(report_date)
        for owner_id in await self._db.fetch_daily_report_users():
            try:
                if not (await self._notifications.get(owner_id))["daily_report"]:
                    continue
                content = await self.build_daily_report(owner_id, report_date)
                await self._db.save_report_and_enqueue_user(
                    owner_id,
                    "daily",
                    period_start,
                    period_end,
                    content,
                    truncate_for_qq(content),
                    self._outbound_max_attempts,
                )
            except Exception:
                logging.getLogger(__name__).exception("日报发送失败 owner=%s", owner_id)

    async def _github_items(self, owner_id: str, period_start: str, period_end: str) -> list[dict]:
        items = []
        for repo in await self._db.fetch_github_repositories(owner_id):
            previous = await self._db.fetch_latest_github_snapshot_before(repo["id"], period_start)
            for snapshot in await self._db.fetch_github_snapshots_between(repo["id"], period_start, period_end):
                changes = compare_snapshots(previous, snapshot)
                if changes:
                    items.append({"repo": repo, "changes": changes})
                previous = snapshot
        return items

    def _period(self, report_date: date) -> tuple[str, str]:
        local_start = datetime.combine(report_date, time.min, tzinfo=self._timezone)
        local_end = local_start + timedelta(days=1)
        return self._utc_string(local_start), self._utc_string(local_end)

    @staticmethod
    def _utc_string(value: datetime) -> str:
        return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")

    def _task_date(self, task: dict) -> date:
        payload = task.get("payload") or task
        value = payload.get("report_date") if isinstance(payload, dict) else None
        if value:
            try:
                return date.fromisoformat(str(value))
            except ValueError as e:
                raise ReportError("日报日期格式应为 YYYY-MM-DD") from e
        return datetime.now(self._timezone).date()


def format_daily_report(report_date: date, github_items: list[dict], tasks: list[dict], memories: list[dict]) -> str:
    lines = [f"【每日总结 {report_date.isoformat()}】", "", "GitHub："]
    if github_items:
        for item in github_items:
            repo = item["repo"]
            lines.append(f'- {repo["repo_owner"]}/{repo["repo_name"]}')
            for change in item["changes"]:
                lines.append(f"  {_format_change(change)}")
    else:
        lines.append("- 暂无变化")

    lines.append("\n任务 / 提醒：")
    if tasks:
        for task in tasks:
            payload = _task_payload(task)
            label = "提醒" if task["task_type"] == "reminder" else task["task_type"]
            lines.append(f'- {label}：{payload.get("message") or "已创建"}')
    else:
        lines.append("- 暂无记录")

    lines.append("\n重要事件：")
    if memories:
        lines.extend(f'- {memory["content"]}' for memory in memories)
    else:
        lines.append("- 暂无记录")
    return "\n".join(lines)


def _format_change(change: dict) -> str:
    if change["type"] == "commit":
        return f'Commit：{change["message"]}（作者：{change["author"]}）'
    if change["type"] == "release":
        return f'Release：{change["to"]}'
    labels = {"stars": "Star", "forks": "Fork", "issues": "开放 Issue/PR"}
    return f'{labels[change["type"]]}：{change["from"]} → {change["to"]}'


def _task_payload(task: dict) -> dict:
    try:
        payload = json.loads(task["payload"])
    except (KeyError, TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}
