"""Fetcher 测试：内容类型白名单、大小上限、redirect 逐跳 SSRF 校验。"""

import asyncio
import sys
from types import ModuleType

import httpx
import pytest

from rumi_helper.security.ssrf import SSRFBlockedError
from rumi_helper.services.web.fetcher import (
    FetchTimeoutError,
    PageTooLargeError,
    PlaywrightFetcher,
    TooManyRedirectsError,
    UnsupportedContentTypeError,
    WebFetcher,
)

HTML = b"<html><title>t</title><body>" + b"x" * 100 + b"</body></html>"


def _resp(status=200, content=b"", headers=None, request=None):
    return httpx.Response(status, content=content, headers=headers or {}, request=request)


async def test_fetch_html_ok():
    def handler(request: httpx.Request) -> httpx.Response:
        return _resp(200, HTML, {"content-type": "text/html; charset=utf-8"}, request)

    f = WebFetcher(transport=httpx.MockTransport(handler))
    result = await f.fetch("https://example.com/page")
    assert result.status_code == 200
    assert "xxxx" in result.text
    assert result.final_url == "https://example.com/page"
    await f.aclose()


async def test_content_type_whitelist():
    def handler(request: httpx.Request) -> httpx.Response:
        return _resp(200, b"binary", {"content-type": "application/octet-stream"}, request)

    f = WebFetcher(transport=httpx.MockTransport(handler))
    with pytest.raises(UnsupportedContentTypeError):
        await f.fetch("https://example.com/file.bin")
    await f.aclose()


async def test_max_bytes_enforced():
    def handler(request: httpx.Request) -> httpx.Response:
        return _resp(200, b"a" * 1000, {"content-type": "text/html"}, request)

    f = WebFetcher(max_bytes=500, transport=httpx.MockTransport(handler))
    with pytest.raises(PageTooLargeError):
        await f.fetch("https://example.com/big")
    await f.aclose()


async def test_redirect_revalidates_ssrf_each_hop(monkeypatch):
    """原始 URL 是公网，重定向跳到内网 —— 第二跳必须被 SSRF 拒绝。"""
    from rumi_helper.security import ssrf

    resolved_hosts: list[str] = []

    async def fake_resolve(host: str):
        resolved_hosts.append(host)
        if host == "example.com":
            return [__import__("ipaddress").ip_address("93.184.216.34")]
        return [__import__("ipaddress").ip_address("10.0.0.5")]  # 内网

    monkeypatch.setattr(ssrf, "resolve_host", fake_resolve)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("host") == "example.com":
            return _resp(302, b"", {"location": "http://internal.example/x"}, request)
        return _resp(200, HTML, {"content-type": "text/html"}, request)

    f = WebFetcher(transport=httpx.MockTransport(handler))
    with pytest.raises(SSRFBlockedError):
        await f.fetch("https://example.com/redirect")
    # 证明第二跳确实做了 DNS 解析 + 校验
    assert "internal.example" in resolved_hosts
    await f.aclose()


async def test_too_many_redirects(monkeypatch):
    import ipaddress

    from rumi_helper.security import ssrf

    async def fake_resolve(host):
        return [ipaddress.ip_address("93.184.216.34")]

    monkeypatch.setattr(ssrf, "resolve_host", fake_resolve)

    def handler(request: httpx.Request) -> httpx.Response:
        return _resp(302, b"", {"location": "https://example.com/loop"}, request)

    f = WebFetcher(max_redirects=3, transport=httpx.MockTransport(handler))
    with pytest.raises(TooManyRedirectsError):
        await f.fetch("https://example.com/start")
    await f.aclose()


async def test_playwright_blocks_private_redirect_before_request(monkeypatch):
    class FakeRequest:
        def __init__(self, url: str):
            self.url = url

    class FakeRoute:
        def __init__(self):
            self.aborted = False
            self.continued = False

        async def abort(self):
            self.aborted = True

        async def continue_(self):
            self.continued = True

    class FakePage:
        url = "https://example.com/page"

        def __init__(self):
            self.handler = None
            self.internal_route = None

        async def route(self, _pattern, handler):
            self.handler = handler

        async def goto(self, *args, **kwargs):
            public_route = FakeRoute()
            await self.handler(public_route, FakeRequest("https://example.com/page"))
            assert public_route.continued is True

            self.internal_route = FakeRoute()
            await self.handler(
                self.internal_route,
                FakeRequest("http://127.0.0.1/internal"),
            )
            assert self.internal_route.aborted is True
            raise RuntimeError("navigation blocked")

        async def content(self):
            raise AssertionError("blocked redirect must not reach page.content()")

    page = FakePage()

    class FakeBrowser:
        async def new_page(self, **kwargs):
            return page

        async def close(self):
            return None

    class FakePlaywright:
        class chromium:
            @staticmethod
            async def launch(**kwargs):
                return FakeBrowser()

    class FakePlaywrightContext:
        async def __aenter__(self):
            return FakePlaywright()

        async def __aexit__(self, *args):
            return None

    async_api = ModuleType("playwright.async_api")
    async_api.Error = RuntimeError
    async_api.TimeoutError = TimeoutError
    async_api.async_playwright = FakePlaywrightContext
    playwright = ModuleType("playwright")
    playwright.async_api = async_api
    monkeypatch.setitem(sys.modules, "playwright", playwright)
    monkeypatch.setitem(sys.modules, "playwright.async_api", async_api)

    checked: list[str] = []

    async def fake_assert_url_safe(url: str):
        checked.append(url)
        if url.startswith("http://127.0.0.1"):
            raise SSRFBlockedError()

    monkeypatch.setattr("rumi_helper.services.web.fetcher.assert_url_safe", fake_assert_url_safe)

    with pytest.raises(SSRFBlockedError):
        await PlaywrightFetcher().fetch("https://example.com/page")
    assert checked[-1] == "http://127.0.0.1/internal"
    assert page.internal_route is not None and page.internal_route.aborted is True


async def test_playwright_navigation_timeout_is_a_fetch_timeout(monkeypatch):
    class FakePlaywrightError(Exception):
        pass

    class FakePlaywrightTimeoutError(FakePlaywrightError):
        pass

    class FakePage:
        async def route(self, *args, **kwargs):
            return None

        async def goto(self, *args, **kwargs):
            raise FakePlaywrightTimeoutError("navigation timed out")

    class FakeBrowser:
        async def new_page(self, **kwargs):
            return FakePage()

        async def close(self):
            return None

    class FakeContext:
        async def __aenter__(self):
            return type("Playwright", (), {"chromium": self})()

        async def __aexit__(self, *args):
            return None

        async def launch(self, **kwargs):
            return FakeBrowser()

    async_api = ModuleType("playwright.async_api")
    async_api.Error = FakePlaywrightError
    async_api.TimeoutError = FakePlaywrightTimeoutError
    async_api.async_playwright = FakeContext
    playwright = ModuleType("playwright")
    playwright.async_api = async_api
    monkeypatch.setitem(sys.modules, "playwright", playwright)
    monkeypatch.setitem(sys.modules, "playwright.async_api", async_api)

    async def allow_url(_url: str):
        return None

    monkeypatch.setattr("rumi_helper.services.web.fetcher.assert_url_safe", allow_url)

    with pytest.raises(FetchTimeoutError, match="navigation timed out"):
        await PlaywrightFetcher().fetch("https://example.com/page")


async def test_concurrent_same_url_shares_one_inflight_request(monkeypatch):
    from rumi_helper.security import ssrf

    async def fake_resolve(_host: str):
        return [__import__("ipaddress").ip_address("93.184.216.34")]

    monkeypatch.setattr(ssrf, "resolve_host", fake_resolve)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _resp(
            200,
            HTML,
            {"content-type": "text/html; charset=utf-8"},
            request,
        )

    fetcher = WebFetcher(
        cache_ttl=0,
        transport=httpx.MockTransport(handler),
    )
    try:
        first, second = await asyncio.gather(
            fetcher.fetch("https://example.com/shared"),
            fetcher.fetch("https://example.com/shared"),
        )
    finally:
        await fetcher.aclose()

    assert calls == 1
    assert first.text == second.text


async def test_fetch_pins_connection_to_validated_ip_and_keeps_host_sni(monkeypatch):
    import ipaddress

    from rumi_helper.security import ssrf

    async def fake_resolve(_host: str):
        return [ipaddress.ip_address("93.184.216.34")]

    monkeypatch.setattr(ssrf, "resolve_host", fake_resolve)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url_host"] = request.url.host
        captured["host_header"] = request.headers.get("host")
        captured["sni"] = request.extensions.get("sni_hostname")
        return _resp(
            200,
            HTML,
            {"content-type": "text/html; charset=utf-8"},
            request,
        )

    fetcher = WebFetcher(transport=httpx.MockTransport(handler))
    try:
        result = await fetcher.fetch("https://example.com/path")
    finally:
        await fetcher.aclose()

    assert result.final_url == "https://example.com/path"
    assert captured == {
        "url_host": "93.184.216.34",
        "host_header": "example.com",
        "sni": "example.com",
    }
