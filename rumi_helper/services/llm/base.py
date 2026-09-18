"""LLM Gateway 抽象接口。任何具体厂商实现都挂在这个接口后面。"""

from abc import ABC, abstractmethod


class LLMError(Exception):
    """LLM 调用失败基类。异常文本不得包含 API Key。"""


class LLMTimeoutError(LLMError):
    pass


class LLMHTTPError(LLMError):
    def __init__(self, status_code: int, detail: str = ""):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"LLM HTTP {status_code}" + (f": {detail}" if detail else ""))


class LLMResponseError(LLMError):
    pass


class LLMProvider(ABC):
    @abstractmethod
    async def chat(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> str:
        """输入 OpenAI 格式 messages，返回助手文本。"""
        raise NotImplementedError

    async def aclose(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def check_health(self) -> None:
        raise NotImplementedError
