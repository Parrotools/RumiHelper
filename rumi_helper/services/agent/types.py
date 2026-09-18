"""Agent tool contracts shared by registry, validation, and orchestration."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

Handler = Callable[[Any, str, dict[str, Any], str], Awaitable[str]]


class AgentArgumentError(ValueError):
    """模型返回的工具参数不符合业务约束。"""


@dataclass(frozen=True)
class AgentTool:
    name: str
    description: str
    parameters: dict[str, Any]
    read_only: bool
    handler: Handler
    requires_admin: bool = False

    def prompt_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "read_only": self.read_only,
            "requires_admin": self.requires_admin,
        }


@dataclass(frozen=True)
class AgentResult:
    """工具层是否接管了本条消息，以及需要发送给用户的内容。"""

    handled: bool
    message: str
