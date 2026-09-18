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
        proxy_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._dns_timeout = max(0.1, float(connect_timeout))
        self._cache_ttl = max(0.0, cache_ttl)
        self._cache: OrderedDict[str, tuple[float, FetchResult]] = OrderedDict()
        self._inflight: dict[str, asyncio.Task[FetchResult]] = {}
        self._headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
        }
        proxy = self._normalize_proxy_url(proxy_url)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(read_timeout, connect=connect_timeout),
            follow_redirects=False,  # 必须手动逐跳校验 SSRF
            proxy=proxy,
            transport=transport,
            trust_env=False,
        )

    @staticmethod
    def _normalize_proxy_url(proxy_url: str | None) -> str | None:
        value = str(proxy_url or "").strip()
        if not value:
            return None
        try:
            parsed = httpx.URL(value)
        except httpx.InvalidURL as exc:
            raise ValueError("WEB_PROXY_URL 无法解析") from exc
        if parsed.scheme != "socks5" or not parsed.host:
            raise ValueError(
                "WEB_PROXY_URL 只支持 socks5://；HTTP CONNECT 无法在 IP pinning 下保留原域名 TLS SNI"
            )
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("WEB_PROXY_URL 只能包含代理地址与可选认证信息，不能包含 path/query/fragment")
        return value

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
            parsed, ips = await resolve_url_safe(
                current,
                dns_timeout=self._dns_timeout,
            )
            host = parsed.host
            host_header = f"[{host}]" if ":" in host else host
            if parsed.port is not None:
                host_header = f"{host_header}:{parsed.port}"
            headers = {**self._headers, "Host": host_header}

            redirected = False
            fetched = False
            last_network_error: httpx.HTTPError | None = None
            # DNS may return duplicate records; only retry distinct validated IPs.
            for ip_text in dict.fromkeys(str(ip) for ip in ips):
                pinned_url = parsed.copy_with(host=ip_text)
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
                            redirected = True
                            break

                        content_type_full = resp.headers.get("content-type", "")
                        m = re.search(r"charset=([\w-]+)", content_type_full, re.IGNORECASE)
                        charset = m.group(1) if m else "utf-8"
                        if status_code >= 400:
                            raise FetchHTTPError(status_code)

                        mime = content_type_full.split(";")[0].strip().lower()
                        if not mime or not _is_supported_content_type(mime):
                            raise UnsupportedContentTypeError(
                                mime or "missing-content-type"
                            )

                        declared = resp.headers.get("content-length")
                        if declared and declared.isdigit() and int(declared) > self._max_bytes:
                            raise PageTooLargeError()

                        buf = bytearray()
                        async for chunk in resp.aiter_bytes():
                            buf.extend(chunk)
                            if len(buf) > self._max_bytes:
                                raise PageTooLargeError()
                        body = bytes(buf)
                        fetched = True
                        break
                except httpx.TimeoutException as exc:
                    last_network_error = exc
                except httpx.HTTPError as exc:
                    last_network_error = exc

            if redirected:
                continue
            if fetched:
                break
            if isinstance(last_network_error, httpx.TimeoutException):
                raise FetchTimeoutError(str(last_network_error)) from last_network_error
            if last_network_error is not None:
                raise FetchNetworkError(str(last_network_error)) from last_network_error
            raise FetchNetworkError("没有可用的已校验目标地址")

        try:
            text = body.decode(charset, errors="replace")
        except LookupError:
            text = body.decode("utf-8", errors="replace")
        result = FetchResult(
            url=url,
            final_url=current,
            status_code=status_code,
            content_type=mime,
            text=text,
        )
        self._cache_put(url, result)
        return result


class PlaywrightFetcher:
    """JS 渲染兜底：默认关闭（ENABLE_PLAYWRIGHT=false），同样必须过 assert_url_safe。

    需要自行安装：pip install .[playwright] && playwright install chromium
    """

    def __init__(
        self,
        max_bytes: int = 4_194_304,
        navigation_timeout: float = 30.0,
        proxy_url: str | None = None,
    ):
        self._max_bytes = max_bytes
        self._timeout_ms = int(navigation_timeout * 1000)
        self._dns_timeout = max(0.1, min(float(navigation_timeout), 10.0))
        self._proxy_url = WebFetcher._normalize_proxy_url(proxy_url)

    def _playwright_proxy_settings(self) -> dict[str, str] | None:
        if not self._proxy_url:
            return None
        parsed = httpx.URL(self._proxy_url)
        host = f"[{parsed.host}]" if ":" in parsed.host else parsed.host
        port = f":{parsed.port}" if parsed.port is not None else ""
        settings = {"server": f"socks5://{host}{port}"}
        if parsed.username:
            settings["username"] = parsed.username
        if parsed.password:
            settings["password"] = parsed.password
        return settings

    async def fetch(self, url: str) -> FetchResult:
        await assert_url_safe(
            url,
            dns_timeout=self._dns_timeout,
        )  # Playwright 一样要过 SSRF 检查
        try:
            from playwright.async_api import Error as PlaywrightError
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise FetchError("Playwright 未安装，无法渲染 JavaScript 网页") from e

        try:
            async with async_playwright() as p:
                launch_kwargs = {"headless": True}
                proxy_settings = self._playwright_proxy_settings()
                if proxy_settings:
                    launch_kwargs["proxy"] = proxy_settings
                browser = await p.chromium.launch(**launch_kwargs)
                try:
                    page = await browser.new_page(user_agent=DEFAULT_USER_AGENT)
                    blocked_error: SSRFBlockedError | None = None

                    async def guard_request(route, request) -> None:
                        nonlocal blocked_error
                        request_url = str(request.url)
                        if request_url.startswith(("http://", "https://")):
                            try:
                                await assert_url_safe(
                                    request_url,
                                    dns_timeout=self._dns_timeout,
                                )
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
                    await assert_url_safe(
                        final_url,
                        dns_timeout=self._dns_timeout,
                    )
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
