"""Whitelist registry for natural-language agent tools."""

from typing import Any

from rumi_helper.services.agent import handlers
from rumi_helper.services.agent.types import AgentTool
from rumi_helper.services.memory import VALID_MEMORY_TYPES


def _empty_parameters() -> dict[str, Any]:
    return {"type": "object", "properties": {}, "additionalProperties": False}


def _repo_parameters() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "完整的 GitHub 仓库 URL 或 owner/repo",
            }
        },
        "required": ["url"],
        "additionalProperties": False,
    }


def _targets_parameters() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "targets": {
                "type": "string",
                "description": "逗号分隔的 user:QQ号 或 group:群号",
            }
        },
        "required": ["targets"],
        "additionalProperties": False,
    }


def _schedule_parameters(content_name: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "group_id": {"type": "string", "description": "数字群号"},
            "timing": {
                "type": "string",
                "description": "YYYY-MM-DD HH:MM 或 cron:0 8 * * *",
            },
            content_name: {"type": "string"},
        },
        "required": ["group_id", "timing", content_name],
        "additionalProperties": False,
    }


def build_tools() -> dict[str, AgentTool]:
    """Return the complete tool whitelist exposed to the planner."""

    return {
        "github_add_repository": AgentTool(
            name="github_add_repository",
            description="将一个 GitHub 仓库加入当前用户的监控列表。",
            parameters=_repo_parameters(),
            read_only=False,
            handler=handlers.github_add,
        ),
        "github_remove_repository": AgentTool(
            name="github_remove_repository",
            description="从当前用户的 GitHub 监控列表移除一个仓库。",
            parameters=_repo_parameters(),
            read_only=False,
            handler=handlers.github_remove,
        ),
        "github_list_repositories": AgentTool(
            name="github_list_repositories",
            description="查看当前用户已经加入的 GitHub 监控仓库。",
            parameters=_empty_parameters(),
            read_only=True,
            handler=handlers.github_list,
        ),
        "github_check_repository": AgentTool(
            name="github_check_repository",
            description="检查当前用户已加入的 GitHub 仓库是否有新变化。",
            parameters=_repo_parameters(),
            read_only=True,
            handler=handlers.github_check,
        ),
        "github_info_repository": AgentTool(
            name="github_info_repository",
            description="查询一个 GitHub 仓库的公开信息。",
            parameters=_repo_parameters(),
            read_only=True,
            handler=handlers.github_info,
        ),
        "github_watch_repository": AgentTool(
            name="github_watch_repository",
            description="给当前用户已加入的 GitHub 仓库添加 user:QQ号 或 group:群号 通知目标。",
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "GitHub 仓库 URL 或 owner/repo",
                    },
                    "target": {
                        "type": "string",
                        "description": "user:QQ号 或 group:群号",
                    },
                },
                "required": ["url", "target"],
                "additionalProperties": False,
            },
            read_only=False,
            handler=handlers.github_watch,
        ),
        "github_digest_set": AgentTool(
            name="github_digest_set",
            description="设置 GitHub 定时汇总的 user:QQ号 或 group:群号 收件人。",
            parameters=_targets_parameters(),
            read_only=False,
            handler=handlers.github_digest_set,
            requires_admin=True,
        ),
        "github_digest_list": AgentTool(
            name="github_digest_list",
            description="查看 GitHub 定时汇总收件人。",
            parameters=_empty_parameters(),
            read_only=True,
            handler=handlers.github_digest_list,
            requires_admin=True,
        ),
        "github_digest_clear": AgentTool(
            name="github_digest_clear",
            description="清空 GitHub 定时汇总收件人并关闭定时汇总。",
            parameters=_empty_parameters(),
            read_only=False,
            handler=handlers.github_digest_clear,
            requires_admin=True,
        ),
        "memory_save": AgentTool(
            name="memory_save",
            description="保存一条当前用户主动要求记住的长期记忆。",
            parameters={
                "type": "object",
                "properties": {
                    "memory_type": {
                        "type": "string",
                        "enum": sorted(VALID_MEMORY_TYPES),
                    },
                    "content": {"type": "string"},
                },
                "required": ["content"],
                "additionalProperties": False,
            },
            read_only=False,
            handler=handlers.memory_save,
        ),
        "memory_list": AgentTool(
            name="memory_list",
            description="查看当前用户的长期记忆。",
            parameters=_empty_parameters(),
            read_only=True,
            handler=handlers.memory_list,
        ),
        "notification_set": AgentTool(
            name="notification_set",
            description="开启或关闭当前用户的 reminder、GitHub 或日报通知。",
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "enum": ["reminder", "github", "report"],
                    },
                    "enabled": {"type": "boolean"},
                },
                "required": ["name", "enabled"],
                "additionalProperties": False,
            },
            read_only=False,
            handler=handlers.notification_set,
        ),
        "reminder_create": AgentTool(
            name="reminder_create",
            description="为当前用户创建一次性或 cron 提醒；timing 必须是 YYYY-MM-DD HH:MM 或 cron:五段表达式。",
            parameters={
                "type": "object",
                "properties": {
                    "timing": {
                        "type": "string",
                        "description": "YYYY-MM-DD HH:MM 或 cron:0 8 * * *",
                    },
                    "message": {"type": "string"},
                },
                "required": ["timing", "message"],
                "additionalProperties": False,
            },
            read_only=False,
            handler=handlers.reminder_create,
        ),
        "schedule_group_message": AgentTool(
            name="schedule_group_message",
            description="管理员创建一次性或 cron 定时群发固定消息。",
            parameters=_schedule_parameters("message"),
            read_only=False,
            handler=handlers.schedule_group_message,
            requires_admin=True,
        ),
        "schedule_topic_joke": AgentTool(
            name="schedule_topic_joke",
            description="管理员创建按主题定时生成不同段子的群任务。",
            parameters=_schedule_parameters("topic"),
            read_only=False,
            handler=handlers.schedule_topic_joke,
            requires_admin=True,
        ),
        "broadcast_message": AgentTool(
            name="broadcast_message",
            description="管理员向一个或多个 user:QQ号、group:群号发送消息。",
            parameters={
                "type": "object",
                "properties": {
                    "targets": {
                        "type": "string",
                        "description": "逗号分隔的 user:QQ号 或 group:群号",
                    },
                    "message": {"type": "string"},
                },
                "required": ["targets", "message"],
                "additionalProperties": False,
            },
            read_only=False,
            handler=handlers.broadcast_message,
            requires_admin=True,
        ),
        "clear_session": AgentTool(
            name="clear_session",
            description="清空当前用户在当前私聊或群聊中的 AI 会话。",
            parameters=_empty_parameters(),
            read_only=False,
            handler=handlers.clear_session,
        ),
        "status": AgentTool(
            name="status",
            description="查看 Bot 运行状态，不包含密钥。",
            parameters=_empty_parameters(),
            read_only=True,
            handler=handlers.status,
        ),
        "daily_report": AgentTool(
            name="daily_report",
            description="查看当前用户的今日日报。",
            parameters=_empty_parameters(),
            read_only=True,
            handler=handlers.daily_report,
        ),
    }
