"""Natural-language planner orchestration for controlled business tools.

The LLM may only choose from the registry. Validation, permissions, confirmation,
and execution remain deterministic Python responsibilities.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from rumi_helper.promptlib import load_prompt
from rumi_helper.services.agent.arguments import (
    missing_arguments_message,
    needs_admin,
    normalize_arguments,
    summarize,
)
from rumi_helper.services.agent.registry import build_tools
from rumi_helper.services.agent.types import AgentArgumentError, AgentResult
from rumi_helper.services.github.client import GitHubAPIError
from rumi_helper.services.github.tracker import GitHubTrackerError
from rumi_helper.services.memory import MemoryValidationError
from rumi_helper.services.notifications import NotificationSettingsError
from rumi_helper.services.qq.broadcast_parser import BroadcastFormatError
from rumi_helper.services.report import ReportError
from rumi_helper.services.scheduler import SchedulerValidationError

logger = logging.getLogger(__name__)

AGENT_SYSTEM_PROMPT = load_prompt("agent.txt")
_AGENT_HINTS = (
    "github", "仓库", "repo", "项目", "添加", "加入", "移除", "删除", "列表", "list", "监控", "检查",
    "记住", "记忆", "memory", "memories", "长期记忆", "提醒", "通知", "定时", "每天", "群发", "广播",
    "发送", "日报", "状态", "清空", "重置会话", "段子", "主题", "消息",
)
_MAX_REQUEST_CHARS = 4000

_SERVICE_ERRORS = (
    GitHubAPIError,
    GitHubTrackerError,
    MemoryValidationError,
    NotificationSettingsError,
    BroadcastFormatError,
    ReportError,
    SchedulerValidationError,
    AgentArgumentError,
    TypeError,
    ValueError,
    json.JSONDecodeError,
)


class AgentHarness:
    """Coordinate planning, validation, confirmation, and tool execution."""

    def __init__(self, *, confirmation_ttl_seconds: int = 300):
        self._confirmation_ttl_seconds = max(30, int(confirmation_ttl_seconds))
        self._tools = build_tools()

    @property
    def tool_names(self) -> frozenset[str]:
        return frozenset(self._tools)

    def looks_like_tool_request(self, text: str) -> bool:
        value = str(text).strip().lower()
        return bool(value) and any(hint in value for hint in _AGENT_HINTS)

    async def try_handle(
        self,
        *,
        runtime: Any,
        event: Any,
        text: str,
        history: list[dict] | None = None,
    ) -> AgentResult | None:
        if not self.looks_like_tool_request(text):
            return None

        plan = await self._plan(runtime, text, history or [])
        if plan is None or plan["action"] == "none":
            return None

        tool = self._tools.get(plan["action"])
        if tool is None:
            return None
        try:
            arguments = normalize_arguments(
                tool.name,
                plan["arguments"],
                runtime,
                history=history or [],
                request_text=text,
            )
        except AgentArgumentError as exc:
            return AgentResult(True, f"参数不合法：{exc}")
        if arguments is None:
            return AgentResult(True, missing_arguments_message(tool.name))

        user_id = str(event.user_id)
        session_key = _session_key(event, runtime)
        if (
            needs_admin(tool, arguments, user_id, session_key)
            and not runtime.permission.is_admin(user_id)
        ):
            return AgentResult(True, "该操作仅管理员可用。")

        if tool.read_only:
            try:
                return AgentResult(True, await tool.handler(runtime, user_id, arguments, session_key))
            except _SERVICE_ERRORS as exc:
                return AgentResult(True, f"操作失败：{exc}")

        summary = summarize(tool.name, arguments)
        expires_at = _utc_now() + timedelta(seconds=self._confirmation_ttl_seconds)
        pending_id = await runtime.db.create_pending_agent_action(
            user_id,
            session_key,
            tool.name,
            json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
            summary,
            expires_at.isoformat(),
        )
        logger.info("agent action pending_id=%s user=%s tool=%s", pending_id, user_id, tool.name)
        minutes = max(1, (self._confirmation_ttl_seconds + 59) // 60)
        return AgentResult(
            True,
            f"我理解为：{summary}\n请在 {minutes} 分钟内回复“确认执行”来执行，或回复“取消执行”。",
        )

    async def has_pending(self, *, runtime: Any, event: Any) -> bool:
        return (
            await runtime.db.fetch_pending_agent_action(
                str(event.user_id), _session_key(event, runtime), _utc_now().isoformat()
            )
        ) is not None

    async def confirm(self, *, runtime: Any, event: Any) -> str:
        user_id = str(event.user_id)
        session_key = _session_key(event, runtime)
        pending = await runtime.db.claim_pending_agent_action(
            user_id, session_key, _utc_now().isoformat()
        )
        if pending is None:
            return "没有待确认的操作（可能已过期、已执行或不存在）。"

        tool = self._tools.get(str(pending["tool_name"]))
        if tool is None:
            await runtime.db.finish_pending_agent_action(pending["id"], "failed")
            return "待确认操作对应的工具已不可用，已取消。"
        try:
            arguments = json.loads(pending["arguments"])
            if not isinstance(arguments, dict):
                raise TypeError("操作参数格式无效")
            if (
                needs_admin(tool, arguments, user_id, session_key)
                and not runtime.permission.is_admin(user_id)
            ):
                raise AgentArgumentError("当前账号已不再具备管理员权限")
            result = await tool.handler(runtime, user_id, arguments, str(pending["session_key"]))
        except _SERVICE_ERRORS as exc:
            await runtime.db.finish_pending_agent_action(pending["id"], "failed")
            logger.warning(
                "agent action failed id=%s tool=%s error=%s",
                pending["id"],
                tool.name,
                exc,
            )
            return f"执行失败：{exc}"
        await runtime.db.finish_pending_agent_action(pending["id"], "confirmed")
        logger.info(
            "agent action confirmed id=%s user=%s tool=%s",
            pending["id"],
            user_id,
            tool.name,
        )
        return result

    async def cancel(self, *, runtime: Any, event: Any) -> str:
        pending = await runtime.db.fetch_pending_agent_action(
            str(event.user_id), _session_key(event, runtime), _utc_now().isoformat()
        )
        if pending is None:
            return "没有待取消的操作。"
        cancelled = await runtime.db.finish_pending_agent_action(pending["id"], "cancelled")
        if not cancelled:
            return "操作已经开始执行，无法取消。"
        return "已取消。"

    async def _plan(
        self,
        runtime: Any,
        text: str,
        history: list[dict],
    ) -> dict[str, Any] | None:
        history_text = "\n".join(
            f"{item.get('role', 'unknown')}: {str(item.get('content', ''))[:1000]}"
            for item in history[-6:]
        )
        tool_text = json.dumps(
            [tool.prompt_schema() for tool in self._tools.values()],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        messages = [
            {
                "role": "system",
                "content": f"{AGENT_SYSTEM_PROMPT}\n<tools>{tool_text}</tools>",
            },
            {
                "role": "user",
                "content": (
                    f"<history>\n{history_text}\n</history>\n"
                    f"<current_request>\n{str(text).strip()[:_MAX_REQUEST_CHARS]}\n</current_request>"
                ),
            },
        ]
        try:
            raw = await runtime.llm.chat(messages, temperature=0.0, max_tokens=400)
        except Exception as exc:  # noqa: BLE001 - planner failure falls back to normal chat
            logger.warning("agent plan failed error=%s", type(exc).__name__)
            return None
        return _parse_plan(raw, self.tool_names)


def _session_key(event: Any, runtime: Any) -> str:
    group_id = getattr(event, "group_id", None)
    if group_id is not None:
        shared = bool(getattr(runtime.settings, "group_shared_context", False))
        return f"group:{group_id}" if shared else f"group:{group_id}:{event.user_id}"
    return f"private:{event.user_id}"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _parse_plan(raw: str, tool_names: frozenset[str]) -> dict[str, Any] | None:
    value = str(raw).strip()
    fence = chr(96) * 3
    if value.startswith(fence) and value.endswith(fence):
        value = value[3:-3].strip()
        if value.lower().startswith("json"):
            value = value[4:].strip()
    try:
        plan = json.loads(value)
    except (TypeError, ValueError):
        return None
    if not isinstance(plan, dict):
        return None
    action = plan.get("action")
    arguments = plan.get("arguments", {})
    if not isinstance(action, str) or not isinstance(arguments, dict):
        return None
    if action != "none" and action not in tool_names:
        return None
    return {"action": action, "arguments": arguments}


__all__ = ["AgentHarness", "AgentResult"]
