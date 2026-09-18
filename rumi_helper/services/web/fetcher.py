"""网页抓取（httpx 异步，手动控制 redirect 以便逐跳做 SSRF 校验）。

带进程内 URL 结果缓存（TTL 可配），同一条链接短时间内重复总结不重复抓取。
"""

import asyncio
import re
import time
from collections import OrderedDict
from dataclasses import dataclass

import httpx

from rumi_helper.security.ssrf import SSRFBlockedError, assert_url_safe, resolve_url_safe

CACHE_MAX_ENTRIES = 16

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 qq-llm-bot/0.1"
)

REDIRECT_STATUSES = {301, 302, 303, 307, 308}

# Content-Type 白名单：只处理网页类内容，拒绝下载任意文件
SUPPORTED_MIME_PREFIXES = ("text/",)
SUPPORTED_MIME_EXACT = {"application/xhtml+xml", "application/xml", "application/json"}


class FetchError(Exception):
    """抓取失败基类。用户侧错误信息由插件层映射。"""


class FetchTimeoutError(FetchError):
    pass


class FetchNetworkError(FetchError):
    pass


class FetchHTTPError(FetchError):
    def __init__(self, status_code: int, detail: str = ""):
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}" + (f": {detail}" if detail else ""))


class TooManyRedirectsError(FetchError):
    pass


class PageTooLargeError(FetchError):
    pass


class UnsupportedContentTypeError(FetchError):
    pass


@dataclass
class FetchResult:
    url: str
    final_url: str
    status_code: int
    content_type: str
    text: str


def _is_supported_content_type(mime: str) -> bool:
    return mime.startswith(SUPPORTED_MIME_PREFIXES) or mime in SUPPORTED_MIME_EXACT


class WebFetcher:
    def __init__(
        self,
        max_bytes: int = 4_194_304,
        connect_timeout: float = 10.0,
        read_timeout: float = 30.0,
        max_redirects: int = 5,
        user_agent: str = DEFAULT_USER_AGENT,
        cache_ttl: float = 600.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._cache_ttl = max(0.0, cache_ttl)
        self._cache: OrderedDict[str, tuple[float, FetchResult]] = OrderedDict()
        self._inflight: dict[str, asyncio.Task[FetchResult]] = {}
        self._headers = {"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5"}
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(read_timeout, connect=connect_timeout),
            follow_redirects=False,  # 必须手动逐跳校验 SSRF
            transport=transport,
        )

    # ---------- URL 结果缓存 ----------

    def _cache_get(self, url: str) -> FetchResult | None:
        if self._cache_ttl <= 0:
            return None
        item = self._cache.get(url)
        if item is None:
            return None
        ts, result = item
        if time.monotonic() - ts > self._cache_ttl:
            del self._cache[url]
            return None
        self._cache.move_to_end(url)
        return result

    def _cache_put(self, url: str, result: FetchResult) -> None:
        if self._cache_ttl <= 0:
            return
        self._cache[url] = (time.monotonic(), result)
        self._cache.move_to_end(url)
        while len(self._cache) > CACHE_MAX_ENTRIES:
            self._cache.popitem(last=False)

    async def aclose(self) -> None:
        tasks = list(self._inflight.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()
        await self._client.aclose()

    async def fetch(self, url: str) -> FetchResult:
        cached = self._cache_get(url)
        if cached is not None:
            return cached

        task = self._inflight.get(url)
        if task is None:
            task = asyncio.create_task(self._fetch_uncached(url))
            self._inflight[url] = task
            task.add_done_callback(
                lambda done, key=url: self._forget_inflight(key, done)
            )
        return await asyncio.shield(task)

    def _forget_inflight(self, url: str, task: asyncio.Task[FetchResult]) -> None:
        if self._inflight.get(url) is task:
            self._inflight.pop(url, None)

    async def _fetch_uncached(self, url: str) -> FetchResult:
        current = url
        redirects = 0
        status_code = 0
        mime = ""
        charset = "utf-8"
        body = b""

        while True:
            # 每一跳都重新解析并校验，并把实际 TCP 连接固定到本次已校验的
            # 公网 IP；Host/SNI 仍使用原域名，避免 DNS rebinding 的 TOCTOU。
            parsed, ips = await resolve_url_safe(current)
            pinned_url = parsed.copy_with(host=str(ips[0]))
            host = parsed.host
            host_header = f"[{host}]" if ":" in host else host
            if parsed.port is not None:
                host_header = f"{host_header}:{parsed.port}"
            headers = {**self._headers, "Host": host_header}
            try:
                async with self._client.stream(
                    "GET",
                    pinned_url,
                    headers=headers,
                    extensions={"sni_hostname": parsed.host},
                ) as resp:
                    status_code = resp.status_code
                    if status_code in REDIRECT_STATUSES:
                        location = resp.headers.get("location")
                        if not location:
                            raise FetchHTTPError(status_code, "重定向缺少 Location")
                        redirects += 1
                        if redirects > self._max_redirects:
                            raise TooManyRedirectsError()
                        current = str(httpx.URL(current).join(location))
                        continue

                    content_type_full = resp.headers.get("content-type", "")
                    m = re.search(r"charset=([\w-]+)", content_type_full, re.IGNORECASE)
                    charset = m.group(1) if m else "utf-8"
                    if status_code >= 400:
                        raise FetchHTTPError(status_code)

                    mime = content_type_full.split(";")[0].strip().lower()
                    if mime and not _is_supported_content_type(mime):
                        raise UnsupportedContentTypeError(mime)

                    declared = resp.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > self._max_bytes:
                        raise PageTooLargeError()

                    buf = bytearray()
                    async for chunk in resp.aiter_bytes():
                        buf.extend(chunk)
                        if len(buf) > self._max_bytes:
                            raise PageTooLargeError()
                    body = bytes(buf)
            except httpx.TimeoutException as e:
                raise FetchTimeoutError(str(e)) from e
            except httpx.HTTPError as e:
                raise FetchNetworkError(str(e)) from e
            break

        result = FetchResult(
            url=url,
            final_url=current,
            status_code=status_code,
            content_type=mime,
            text=body.decode(charset, errors="replace"),
        )
        self._cache_put(url, result)
        return result


class PlaywrightFetcher:
    """JS 渲染兜底：默认关闭（ENABLE_PLAYWRIGHT=false），同样必须过 assert_url_safe。

    需要自行安装：pip install .[playwright] && playwright install chromium
    """

    def __init__(self, max_bytes: int = 4_194_304, navigation_timeout: float = 30.0):
        self._max_bytes = max_bytes
        self._timeout_ms = int(navigation_timeout * 1000)

    async def fetch(self, url: str) -> FetchResult:
        await assert_url_safe(url)  # Playwright 一样要过 SSRF 检查
        try:
            from playwright.async_api import Error as PlaywrightError
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise FetchError("Playwright 未安装，无法渲染 JavaScript 网页") from e

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                try:
                    page = await browser.new_page(user_agent=DEFAULT_USER_AGENT)
                    blocked_error: SSRFBlockedError | None = None

                    async def guard_request(route, request) -> None:
                        nonlocal blocked_error
                        request_url = str(request.url)
                        if request_url.startswith(("http://", "https://")):
                            try:
                                await assert_url_safe(request_url)
                            except SSRFBlockedError as exc:
                                blocked_error = exc
                                await route.abort()
                                return
                        await route.continue_()

                    await page.route("**/*", guard_request)
                    try:
                        await page.goto(
                            url,
                            timeout=self._timeout_ms,
                            wait_until="domcontentloaded",
                        )
                    except PlaywrightError as exc:
                        if blocked_error is not None:
                            raise blocked_error from exc
                        raise
                    if blocked_error is not None:
                        raise blocked_error
                    final_url = page.url
                    await assert_url_safe(final_url)
                    html = await page.content()
                finally:
                    await browser.close()
        except PlaywrightTimeoutError as e:
            raise FetchTimeoutError(str(e)) from e
        except PlaywrightError as e:
            raise FetchNetworkError(str(e)) from e

        if len(html.encode("utf-8")) > self._max_bytes:
            raise PageTooLargeError()
        return FetchResult(url=url, final_url=final_url, status_code=200, content_type="text/html", text=html)
