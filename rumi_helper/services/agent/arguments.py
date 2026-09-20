"""Argument normalization and confirmation summaries for AgentHarness."""

import re
from collections.abc import Mapping
from typing import Any
from zoneinfo import ZoneInfo

from rumi_helper.security.identifiers import is_ascii_decimal_id
from rumi_helper.security.permissions import (
    notification_target_requires_admin,
    shared_session_clear_requires_admin,
)
from rumi_helper.services.agent.types import AgentArgumentError, AgentTool
from rumi_helper.services.github.tracker import GitHubTrackerError, parse_repo_url
from rumi_helper.services.memory import VALID_MEMORY_TYPES
from rumi_helper.services.qq.broadcast_parser import BroadcastFormatError, parse_targets
from rumi_helper.services.scheduler import SchedulerValidationError, parse_reminder_command
from rumi_helper.services.web.url_parser import extract_urls
from rumi_helper.utils import QQ_TEXT_SAFE_LIMIT, truncate_for_qq

_GITHUB_REPO_SHORTHAND = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_MAX_AGENT_TEXT_CHARS = 2000
_EMPTY_TOOLS = frozenset(
    {
        "github_list_repositories",
        "github_digest_list",
        "github_digest_clear",
        "memory_list",
        "clear_session",
        "status",
        "daily_report",
    }
)
_REPO_REFERENTS = frozenset(
    {"这个仓库", "该仓库", "上面那个仓库", "刚才那个仓库", "这个repo", "该repo", "the repo", "it"}
)
_NORMALIZED_REPO_REFERENTS = frozenset("".join(item.lower().split()) for item in _REPO_REFERENTS)


def normalize_arguments(
    tool_name: str,
    arguments: Mapping[str, Any],
    runtime: Any,
    *,
    history: list[dict] | None = None,
    request_text: str = "",
) -> dict[str, Any] | None:
    if not isinstance(arguments, Mapping):
        return None
    if tool_name in _EMPTY_TOOLS:
        return {} if not arguments else None
    if tool_name in {
        "github_add_repository",
        "github_remove_repository",
        "github_check_repository",
        "github_info_repository",
    }:
        return {"url": _normalize_repo(arguments, history=history, request_text=request_text)}
    if tool_name == "github_watch_repository":
        if set(arguments) != {"url", "target"}:
            return None
        url = _normalize_repo(
            {"url": arguments.get("url")},
            history=history,
            request_text=request_text,
        )
        target = _normalize_target(arguments.get("target"))
        return {"url": url, "target": target}
    if tool_name == "github_digest_set":
        if set(arguments) != {"targets"}:
            return None
        return {"targets": _normalize_targets(arguments, runtime)}
    if tool_name == "memory_save":
        if set(arguments) - {"memory_type", "content"} or "content" not in arguments:
            return None
        content = str(arguments["content"]).strip()
        memory_type = str(arguments.get("memory_type", "fact") or "fact").strip().lower()
        if not content or memory_type not in VALID_MEMORY_TYPES:
            raise AgentArgumentError("记忆类型或内容无效")
        if len(content) > _MAX_AGENT_TEXT_CHARS:
            raise AgentArgumentError(f"记忆内容不能超过 {_MAX_AGENT_TEXT_CHARS} 个字符")
        return {"memory_type": memory_type, "content": content}
    if tool_name == "notification_set":
        if set(arguments) != {"name", "enabled"}:
            return None
        names = {
            "reminder": "reminder_notify",
            "github": "github_notify",
            "report": "daily_report",
        }
        name = str(arguments["name"]).strip().lower()
        enabled = arguments["enabled"]
        if name not in names or not isinstance(enabled, bool):
            raise AgentArgumentError("通知项应为 reminder、github 或 report，enabled 必须是布尔值")
        return {"field": names[name], "enabled": enabled}
    if tool_name == "reminder_create":
        return _normalize_timing_and_text(arguments, runtime, "message")
    if tool_name in {"schedule_group_message", "schedule_topic_joke"}:
        text_key = "message" if tool_name == "schedule_group_message" else "topic"
        if set(arguments) != {"group_id", "timing", text_key}:
            return None
        group_id = str(arguments["group_id"]).strip()
        content = str(arguments[text_key]).strip()
        timing = str(arguments["timing"]).strip()
        if not is_ascii_decimal_id(group_id) or not content:
            raise AgentArgumentError("群号必须是数字，内容不能为空")
        _validate_timing(timing, content, runtime)
        return {"group_id": group_id, "timing": timing, text_key: content}
    if tool_name == "broadcast_message":
        if set(arguments) != {"targets", "message"}:
            return None
        message = str(arguments["message"]).strip()
        if not message:
            raise AgentArgumentError("消息内容不能为空")
        if len(message) > QQ_TEXT_SAFE_LIMIT:
            raise AgentArgumentError(
                f"消息内容不能超过 {QQ_TEXT_SAFE_LIMIT} 个字符"
            )
        targets = _normalize_targets(arguments, runtime)
        return {"targets": targets, "message": message}
    return None


def needs_admin(
    tool: AgentTool,
    arguments: Mapping[str, Any],
    user_id: str | None = None,
    session_key: str | None = None,
) -> bool:
    if tool.requires_admin:
        return True
    if tool.name == "clear_session":
        return bool(
            session_key
            and shared_session_clear_requires_admin(session_key)
        )
    if tool.name != "github_watch_repository":
        return False

    target_type, separator, target_id = str(arguments.get("target", "")).partition(":")
    if not separator:
        return False
    return (
        user_id is not None
        and notification_target_requires_admin(user_id, target_type, target_id)
    )


def missing_arguments_message(tool_name: str) -> str:
    messages = {
        "github": "请提供完整的 GitHub 仓库链接或 owner/repo。",
        "memory_save": "请告诉我要记住的具体内容。",
        "notification_set": "请说明要开启或关闭哪类通知。",
        "reminder_create": "请提供提醒时间和内容，例如“明天 8 点提醒我提交周报”。",
        "schedule_group_message": "请提供群号、时间和要发送的内容。",
        "schedule_topic_joke": "请提供群号、时间和段子主题。",
        "broadcast_message": "请提供发送目标和消息内容。",
    }
    if tool_name.startswith("github_"):
        return messages["github"]
    return messages.get(tool_name, "我需要更明确的信息才能执行这个操作。")


def summarize(tool_name: str, arguments: dict[str, Any]) -> str:
    summaries = {
        "github_add_repository": "把 {url} 加入你的 GitHub 监控列表",
        "github_remove_repository": "从你的 GitHub 监控列表移除 {url}",
        "github_watch_repository": "为 {url} 添加通知目标 {target}",
        "github_digest_set": "把 GitHub 定时汇总发送给 {targets}",
        "github_digest_clear": "清空 GitHub 定时汇总收件人并关闭定时汇总",
        "memory_save": "保存一条长期记忆：{content}",
        "notification_set": "{field}通知{enabled_label}",
        "reminder_create": "创建提醒：{timing} —— {message}",
        "schedule_group_message": "向群 {group_id} 按 {timing} 发送：{message}",
        "schedule_topic_joke": "在群 {group_id} 按 {timing} 讲 {topic} 主题段子",
        "broadcast_message": "向 {targets} 发送消息：{message}",
        "clear_session": "清空当前 AI 会话",
    }
    value = dict(arguments)
    if tool_name == "notification_set":
        value["enabled_label"] = "开启" if value["enabled"] else "关闭"
        value["field"] = {
            "reminder_notify": "提醒",
            "github_notify": "GitHub",
            "daily_report": "日报",
        }[value["field"]]
    template = summaries.get(tool_name, f"执行 {tool_name}")
    return truncate_for_qq(template.format(**value), 800)


def _normalize_repo(
    arguments: Mapping[str, Any],
    *,
    history: list[dict] | None = None,
    request_text: str = "",
) -> str:
    if set(arguments) - {"url"}:
        raise AgentArgumentError("需要一个 GitHub 仓库 URL 或 owner/repo")
    value = str(arguments.get("url", "")).strip()
    if not value or _is_repo_referent(value):
        value = _latest_github_url([*(history or []), {"role": "user", "content": request_text}]) or ""
    else:
        urls = extract_urls(value)
        if urls:
            value = urls[0]
    if _GITHUB_REPO_SHORTHAND.fullmatch(value):
        value = f"https://github.com/{value}"
    try:
        return parse_repo_url(value).url
    except GitHubTrackerError as exc:
        raise AgentArgumentError(str(exc)) from exc


def _normalize_target(value: Any) -> str:
    target = str(value or "").strip()
    target_type, separator, target_id = target.partition(":")
    if (
        not separator
        or target_type.lower() not in {"user", "group"}
        or not is_ascii_decimal_id(target_id)
    ):
        raise AgentArgumentError("通知目标应为 user:QQ号 或 group:群号")
    return f"{target_type.lower()}:{target_id}"


def _normalize_targets(arguments: Mapping[str, Any], runtime: Any) -> str:
    raw = str(arguments.get("targets", "")).strip()
    try:
        targets = parse_targets(raw)
    except BroadcastFormatError as exc:
        raise AgentArgumentError(str(exc)) from exc
    if len(targets) > runtime.settings.max_broadcast_recipients:
        raise AgentArgumentError(
            f"目标数量 {len(targets)} 超过上限 {runtime.settings.max_broadcast_recipients}"
        )
    return ",".join(target.display() for target in targets)


def _normalize_timing_and_text(
    arguments: Mapping[str, Any],
    runtime: Any,
    text_key: str,
) -> dict[str, str] | None:
    if set(arguments) != {"timing", text_key}:
        return None
    timing = str(arguments["timing"]).strip()
    content = str(arguments[text_key]).strip()
    if not timing or not content:
        raise AgentArgumentError("时间和内容不能为空")
    _validate_timing(timing, content, runtime)
    return {"timing": timing, text_key: content}


def _validate_timing(timing: str, content: str, runtime: Any) -> None:
    try:
        timezone_info = ZoneInfo(runtime.settings.scheduler_timezone)
        parse_reminder_command(f"/remind {timing} -- {content}", timezone_info)
    except (SchedulerValidationError, ValueError) as exc:
        raise AgentArgumentError(str(exc)) from exc


def _is_repo_referent(value: str) -> bool:
    normalized = "".join(value.lower().split())
    return normalized in _NORMALIZED_REPO_REFERENTS


def _latest_github_url(history: list[dict]) -> str | None:
    for item in reversed(history):
        if item.get("role") != "user":
            continue
        for url in reversed(extract_urls(str(item.get("content", "")))):
            try:
                return parse_repo_url(url).url
            except GitHubTrackerError:
                continue
    return None
