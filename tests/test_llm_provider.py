import asyncio

import httpx
import pytest

from app.services.llm.base import LLMHTTPError, LLMResponseError, LLMTimeoutError
from app.services.llm.openai_compatible import OpenAICompatibleProvider

BASE = "http://llm.test/v1"


def _ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": "你好！"}}]})


async def test_chat_success():
    provider = OpenAICompatibleProvider(BASE, "sk-test", "m1", transport=httpx.MockTransport(_ok_handler))
    out = await provider.chat([{"role": "user", "content": "hi"}])
    assert out == "你好！"
    await provider.aclose()


async def test_chat_sends_model_and_auth():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = request.read()
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    provider = OpenAICompatibleProvider(BASE, "sk-secret", "m1", transport=httpx.MockTransport(handler))
    await provider.chat([{"role": "user", "content": "hi"}])
    assert captured["auth"] == "Bearer sk-secret"
    assert b'"model":"m1"' in captured["body"].replace(b" ", b"") or b'"model": "m1"' in captured["body"]
    await provider.aclose()


async def test_http_error():
    provider = OpenAICompatibleProvider(
        BASE, "k", "m", transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom"))
    )
    with pytest.raises(LLMHTTPError) as ei:
        await provider.chat([{"role": "user", "content": "hi"}])
    assert ei.value.status_code == 500
    await provider.aclose()


async def test_invalid_json():
    provider = OpenAICompatibleProvider(
        BASE, "k", "m", transport=httpx.MockTransport(lambda r: httpx.Response(200, text="not json{"))
    )
    with pytest.raises(LLMResponseError):
        await provider.chat([{"role": "user", "content": "hi"}])
    await provider.aclose()


async def test_empty_choices():
    provider = OpenAICompatibleProvider(
        BASE, "k", "m", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"choices": []}))
    )
    with pytest.raises(LLMResponseError):
        await provider.chat([{"role": "user", "content": "hi"}])
    await provider.aclose()


async def test_empty_content():
    provider = OpenAICompatibleProvider(
        BASE, "k", "m",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})),
    )
    with pytest.raises(LLMResponseError):
        await provider.chat([{"role": "user", "content": "hi"}])
    await provider.aclose()


async def test_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    provider = OpenAICompatibleProvider(BASE, "k", "m", transport=httpx.MockTransport(handler))
    with pytest.raises(LLMTimeoutError):
        await provider.chat([{"role": "user", "content": "hi"}])
    await provider.aclose()


async def test_health_check_respects_same_concurrency_limit_as_chat():
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingTransport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.calls = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            self.calls += 1
            if self.calls == 1:
                entered.set()
                await release.wait()
                return httpx.Response(
                    200,
                    json={"choices": [{"message": {"content": "ok"}}]},
                    request=request,
                )
            return httpx.Response(200, json={"data": []}, request=request)

    transport = BlockingTransport()
    provider = OpenAICompatibleProvider(
        BASE,
        "k",
        "m",
        max_concurrency=1,
        transport=transport,
    )

    chat_task = asyncio.create_task(
        provider.chat([{"role": "user", "content": "hi"}])
    )
    await entered.wait()
    health_task = asyncio.create_task(provider.check_health())
    await asyncio.sleep(0)

    assert transport.calls == 1
    assert health_task.done() is False

    release.set()
    await chat_task
    await health_task
    assert transport.calls == 2

    await provider.aclose()
