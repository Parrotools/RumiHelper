from types import SimpleNamespace

import pytest
from nonebot.adapters.onebot.v11 import Message, PrivateMessageEvent
from nonebot.adapters.onebot.v11.event import Sender

from rumi_helper.plugins import web_summary
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


class RecordingSessions:
    def __init__(self):
        self.rows: list[tuple[str, str, str]] = []

    async def append(self, session_key: str, role: str, content: str):
        self.rows.append((session_key, role, content))


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


async def test_web_summary_session_history_redacts_url_secrets(monkeypatch):
    sessions = RecordingSessions()
    runtime = SimpleNamespace(
        settings=SimpleNamespace(group_shared_context=False),
        sessions=sessions,
    )
    summarized: list[str] = []

    async def fake_summarize_one(_matcher, _runtime, url: str, notice: bool = False):
        summarized.append(url)

    monkeypatch.setattr(web_summary, "get_runtime", lambda: runtime)
    monkeypatch.setattr(web_summary, "_summarize_one", fake_summarize_one)

    secret = "super-secret-query-token"
    raw = (
        "https://user:password@github.com/OpenAI/openai-python"
        f"?token={secret}&signature=abc#fragment"
    )
    event = PrivateMessageEvent(
        time=0,
        self_id="10000",
        post_type="message",
        sub_type="friend",
        user_id=20000,
        message_type="private",
        message_id=1,
        message=Message(raw),
        raw_message=raw,
        font=0,
        sender=Sender(user_id=20000, nickname="u"),
    )

    await web_summary._handle(event, SimpleNamespace())

    assert summarized == [raw]
    assert len(sessions.rows) == 1
    _session_key, role, stored = sessions.rows[0]
    assert role == "user"
    assert stored == "https://github.com/OpenAI/openai-python"
    assert secret not in stored
    assert "password" not in stored
    assert "signature=abc" not in stored
