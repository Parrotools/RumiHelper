"""OpenAI Compatible LLM 实现（httpx 异步），配置全部来自环境变量。"""

import asyncio

import httpx

from app.services.llm.base import LLMError, LLMHTTPError, LLMProvider, LLMResponseError, LLMTimeoutError


class OpenAICompatibleProvider(LLMProvider):
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 60.0,
        max_concurrency: int = 5,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=min(timeout, 15.0)),
            transport=transport,
        )

    async def chat(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> str:
        payload: dict = {"model": self._model, "messages": messages, "temperature": temperature}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        url = f"{self._base_url}/chat/completions"

        async with self._semaphore:
            try:
                resp = await self._client.post(url, json=payload, headers=headers)
            except httpx.TimeoutException as e:
                raise LLMTimeoutError("LLM 请求超时") from e
            except httpx.HTTPError as e:
                raise LLMError(f"LLM 请求失败：{type(e).__name__}") from e

        if resp.status_code != 200:
            raise LLMHTTPError(resp.status_code, resp.text[:200])

        try:
            data = resp.json()
        except ValueError as e:
            raise LLMResponseError("LLM 返回了无效 JSON") from e

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMResponseError("LLM 返回缺少 choices/message 内容") from e

        if not isinstance(content, str) or not content.strip():
            raise LLMResponseError("LLM 返回了空内容")
        return content.strip()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def check_health(self) -> None:
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        async with self._semaphore:
            try:
                response = await self._client.get(
                    f"{self._base_url}/models",
                    headers=headers,
                )
            except httpx.TimeoutException as e:
                raise LLMTimeoutError("LLM 请求超时") from e
            except httpx.HTTPError as e:
                raise LLMError(f"LLM 请求失败：{type(e).__name__}") from e
        if response.status_code >= 400:
            raise LLMHTTPError(response.status_code)
