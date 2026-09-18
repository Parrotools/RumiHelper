"""网页总结插件。

触发方式：
1. /总结 <URL> 或 /summary <URL>（可一次给多个 URL，最多处理 3 个）
2. 自动模式（URL_AUTO_SUMMARY_MODE）：
   - off：不自动读取，只响应明确命令
   - mentioned：被 @ / 私聊 / 回复机器人 且消息含 URL 时自动处理
   - all：任何消息检测到 URL 就处理

抓取失败且 ENABLE_PLAYWRIGHT=true 时自动降级到 Playwright 渲染。
"""

from nonebot import logger, on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent, PrivateMessageEvent
from nonebot.matcher import Matcher
from nonebot.rule import Rule

from app.plugins.ai_chat import claim_message_id, command_is_addressed, strip_bot_mention
from app.security.ssrf import SSRFBlockedError
from app.services.llm.base import LLMError
from app.services.runtime import Runtime, get_runtime
from app.services.session.manager import SessionManager
from app.services.web.extractor import ExtractionError, extract
from app.services.web.fetcher import (
    FetchError,
    FetchTimeoutError,
    PageTooLargeError,
    TooManyRedirectsError,
    UnsupportedContentTypeError,
)
from app.services.web.url_parser import extract_urls
from app.utils import send_local_reply, send_reply, truncate_for_qq

SUMMARY_COMMANDS = ("/总结", "/summary")
MAX_URLS_PER_MESSAGE = 3
_GITHUB_AGENT_HINTS = (
    "添加", "加入", "移除", "删除", "监控", "列表", "检查", "查询", "查看信息", "watch", "add", "remove", "check", "info",
)


def _strip_command(text: str) -> str:
    for cmd in SUMMARY_COMMANDS:
        if text.startswith(cmd):
            return text[len(cmd):].strip()
    return text


def _is_self(event: MessageEvent) -> bool:
    return str(event.user_id) == str(event.self_id)


async def _trigger(event: MessageEvent) -> bool:
    if _is_self(event):
        return False
    text = strip_bot_mention(event.message.extract_plain_text())
    is_command = any(text.startswith(cmd) for cmd in SUMMARY_COMMANDS)
    if isinstance(event, GroupMessageEvent) and is_command and not command_is_addressed(event):
        return False
    # 其他斜杠命令（例如 /github add <URL>）交给对应命令插件，不能被 URL 自动总结抢走。
    if text.startswith("/") and not is_command:
        return False
    # 带 GitHub URL 的明确操作交给 Agent Harness；否则“把这个仓库加入列表”会先被网页总结抢走。
    if not is_command and _looks_like_github_operation(text):
        return False
    mode = get_runtime().settings.url_auto_summary_mode

    if is_command:
        matched = True  # 命令即使没带 URL 也要接住，回复用法提示
    elif mode == "off":
        matched = False
    elif mode == "all":
        matched = bool(extract_urls(text))
    else:  # mentioned：仅私聊 / 被@ / 回复机器人
        if isinstance(event, PrivateMessageEvent):
            matched = bool(extract_urls(text))
        elif isinstance(event, GroupMessageEvent):
            matched = event.to_me and bool(extract_urls(text))
        else:
            matched = False

    if matched:
        return claim_message_id(str(event.message_id))
    return False


matcher = on_message(rule=Rule(_trigger), priority=6, block=True)


@matcher.handle()
async def _handle(event: MessageEvent, matcher_: Matcher):
    runtime = get_runtime()
    text = strip_bot_mention(event.message.extract_plain_text())

    is_command = any(text.startswith(cmd) for cmd in SUMMARY_COMMANDS)
    body = _strip_command(text) if is_command else text
    urls = extract_urls(body) or extract_urls(text)
    if not urls:
        await send_local_reply(matcher_, runtime, "用法：/总结 <网页URL>（可一次给多个，最多同时处理 3 个）")
        return

    # 网页总结不进入 AI 回复流程，但保留用户发过的 URL，供下一句“这个仓库”做确定性指代解析。
    await runtime.sessions.append(_session_key(event, runtime), "user", text)

    for url in urls[:MAX_URLS_PER_MESSAGE]:
        await _summarize_one(matcher_, runtime, url, notice=is_command)


def _looks_like_github_operation(text: str) -> bool:
    urls = extract_urls(text)
    return bool(
        any("github.com" in url.lower() for url in urls)
        and any(hint in text.lower() for hint in _GITHUB_AGENT_HINTS)
    )


def _session_key(event: MessageEvent, runtime: Runtime) -> str:
    if isinstance(event, GroupMessageEvent):
        return SessionManager.group_key(
            str(event.group_id), str(event.user_id), runtime.settings.group_shared_context
        )
    return SessionManager.private_key(str(event.user_id))


async def _fetch_document(runtime: Runtime, url: str):
    """httpx 抓取 + 提取；失败时按配置降级 Playwright（SSRF 拦截不降级）。"""
    try:
        result = await runtime.fetcher.fetch(url)
        return extract(result.text, result.final_url)
    except (SSRFBlockedError, PageTooLargeError, UnsupportedContentTypeError, TooManyRedirectsError):
        # Security/resource policy decisions must not be bypassed by a browser fallback.
        raise
    except (FetchError, ExtractionError):
        if runtime.playwright_fetcher is None:
            raise
        logger.info("普通抓取失败，尝试 Playwright fallback url=%s", url)
        pw = await runtime.playwright_fetcher.fetch(url)
        return extract(pw.text, pw.final_url)


async def _summarize_one(matcher_: Matcher, runtime: Runtime, url: str, notice: bool = False) -> None:
    logger.info("网页总结 url=%s", url)
    if notice:
        await send_local_reply(matcher_, runtime, f"正在读取网页：{url}")
    try:
        async with runtime.web_semaphore:  # 并发限制，防止慢网页拖死 Bot
            doc = await _fetch_document(runtime, url)
            summary = await runtime.summarizer.summarize(doc)
        await send_reply(matcher_, runtime, truncate_for_qq(summary))
    except SSRFBlockedError:
        await send_local_reply(matcher_, runtime, "该地址不被允许访问（内网或保留地址）。")
    except FetchTimeoutError:
        await send_local_reply(matcher_, runtime, "无法访问该网页：请求超时。")
    except PageTooLargeError:
        await send_local_reply(matcher_, runtime, "网页过大，无法处理。")
    except UnsupportedContentTypeError:
        await send_local_reply(matcher_, runtime, "该链接不是网页内容，暂不支持。")
    except FetchError:
        logger.exception("网页抓取失败 url=%s", url)
        await send_local_reply(matcher_, runtime, "无法访问该网页。")
    except ExtractionError:
        await send_local_reply(matcher_, runtime, "网页可以访问，但没有提取到有效正文。")
    except LLMError:
        logger.exception("网页总结 LLM 调用失败 url=%s", url)
        await send_local_reply(matcher_, runtime, "AI 服务暂时不可用，请稍后再试。")
