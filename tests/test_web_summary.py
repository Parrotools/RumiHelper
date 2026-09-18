from types import SimpleNamespace

import pytest

from rumi_helper.plugins.web_summary import _fetch_document
from rumi_helper.security.ssrf import SSRFBlockedError
from rumi_helper.services.web.fetcher import (
    PageTooLargeError,
    TooManyRedirectsError,
    UnsupportedContentTypeError,
)


class RaisingFetcher:
    def __init__(self, error: Exception):
        self.error = error

    async def fetch(self, url: str):
        raise self.error


class RecordingPlaywright:
    def __init__(self):
        self.calls: list[str] = []

    async def fetch(self, url: str):
        self.calls.append(url)
        return SimpleNamespace(
            text="<html><body>" + "rendered content " * 10 + "</body></html>",
            final_url=url,
        )


@pytest.mark.parametrize(
    "error",
    [
        PageTooLargeError(),
        UnsupportedContentTypeError("application/octet-stream"),
        TooManyRedirectsError(),
        SSRFBlockedError("blocked"),
    ],
)
async def test_policy_rejections_never_fall_back_to_playwright(error):
    playwright = RecordingPlaywright()
    runtime = SimpleNamespace(
        fetcher=RaisingFetcher(error),
        playwright_fetcher=playwright,
    )

    with pytest.raises(type(error)):
        await _fetch_document(runtime, "https://example.com/resource")

    assert playwright.calls == []
