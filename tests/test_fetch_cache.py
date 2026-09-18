"""WebFetcher URL 缓存测试。"""

import httpx

from rumi_helper.services.web.fetcher import WebFetcher

HTML = b"<html><body>hello</body></html>"


def _counting_transport(counter: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        return httpx.Response(200, content=HTML, headers={"content-type": "text/html"}, request=request)

    return httpx.MockTransport(handler)


async def test_cache_hit_within_ttl():
    counter = {"n": 0}
    f = WebFetcher(cache_ttl=600, transport=_counting_transport(counter))
    r1 = await f.fetch("https://example.com/a")
    r2 = await f.fetch("https://example.com/a")
    assert counter["n"] == 1
    assert r1 is r2  # 命中缓存返回同一对象
    await f.aclose()


async def test_cache_disabled():
    counter = {"n": 0}
    f = WebFetcher(cache_ttl=0, transport=_counting_transport(counter))
    await f.fetch("https://example.com/a")
    await f.fetch("https://example.com/a")
    assert counter["n"] == 2
    await f.aclose()


async def test_cache_different_urls():
    counter = {"n": 0}
    f = WebFetcher(cache_ttl=600, transport=_counting_transport(counter))
    await f.fetch("https://example.com/a")
    await f.fetch("https://example.com/b")
    assert counter["n"] == 2
    await f.aclose()


async def test_failures_not_cached():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x", headers={"content-type": "application/octet-stream"}, request=request)

    from rumi_helper.services.web.fetcher import UnsupportedContentTypeError

    f = WebFetcher(cache_ttl=600, transport=httpx.MockTransport(handler))
    for _ in range(2):
        try:
            await f.fetch("https://example.com/bin")
        except UnsupportedContentTypeError:
            pass
    # 失败结果不缓存，因此不会出现"第二次直接返回缓存"的假象
    assert f._cache_get("https://example.com/bin") is None
    await f.aclose()
