"""运行时容器：所有服务的单例装配点。

bot.py 启动时调用 init_runtime()，关闭时调用 close_runtime()；
插件通过 get_runtime() 取用。
"""

import asyncio
import logging
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field

from rumi_helper.config import Settings, get_settings
from rumi_helper.database.db import Database
from rumi_helper.personality.manager import PersonalityManager
from rumi_helper.security.permissions import PermissionService
from rumi_helper.services.agent import AgentHarness
from rumi_helper.services.github.client import GitHubClient
from rumi_helper.services.github.tracker import GitHubTracker
from rumi_helper.services.group_feedback import GroupFeedbackService
from rumi_helper.services.health import HealthService
from rumi_helper.services.llm.base import LLMProvider
from rumi_helper.services.llm.openai_compatible import OpenAICompatibleProvider
from rumi_helper.services.memory import MemoryService
from rumi_helper.services.notifications import NotificationSettingsService
from rumi_helper.services.qq.dispatcher import MessageDispatcher
from rumi_helper.services.report import ReportService
from rumi_helper.services.scheduler import SchedulerService
from rumi_helper.services.session.manager import SessionManager
from rumi_helper.services.web.extractor import ExtractionError  # noqa: F401  便于插件统一导入
from rumi_helper.services.web.fetcher import PlaywrightFetcher, WebFetcher
from rumi_helper.services.web.summarizer import WebSummarizer
from rumi_helper.utils import ReplyGate


@dataclass
class Runtime:
    settings: Settings
    db: Database
    personality: PersonalityManager
    memory: MemoryService
    notifications: NotificationSettingsService
    group_feedback: GroupFeedbackService
    scheduler: SchedulerService
    github_client: GitHubClient
    github: GitHubTracker
    health: HealthService
    report: ReportService
    sessions: SessionManager
    llm: LLMProvider
    dispatcher: MessageDispatcher
    permission: PermissionService
    fetcher: WebFetcher
    playwright_fetcher: PlaywrightFetcher | None
    summarizer: WebSummarizer
    web_semaphore: asyncio.Semaphore
    agent: AgentHarness
    reply_gate: ReplyGate = field(default_factory=ReplyGate)


_runtime: Runtime | None = None
_runtime_lock = asyncio.Lock()


async def init_runtime() -> Runtime:
    """Initialize at most one Runtime even if startup is requested concurrently."""
    async with _runtime_lock:
        return await _init_runtime_unlocked()


async def _init_runtime_unlocked() -> Runtime:
    global _runtime
    if _runtime is not None:
        return _runtime

    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    # Keep ownership local until every startup step succeeds. If any constructor,
    # migration, task sync, or worker start fails, AsyncExitStack unwinds all
    # resources and no half-initialized Runtime is published globally.
    async with AsyncExitStack() as stack:
        db = Database(settings.database_path)
        await db.connect()
        stack.push_async_callback(db.close)

        personality = PersonalityManager(settings.personality_file)

        llm = OpenAICompatibleProvider(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            timeout=settings.llm_timeout,
            max_concurrency=settings.max_concurrent_llm_tasks,
        )
        stack.push_async_callback(llm.aclose)

        dispatcher = MessageDispatcher(
            db,
            rate_limit_per_second=settings.send_rate_limit_per_second,
            max_attempts=settings.outbound_max_attempts,
            retry_delay_seconds=settings.outbound_retry_delay_seconds,
            account_pause_seconds=settings.outbound_account_pause_seconds,
            rate_limit_pause_seconds=settings.outbound_rate_limit_pause_seconds,
            queue_poll_seconds=settings.outbound_queue_poll_seconds,
            lease_seconds=settings.outbound_lease_seconds,
        )
        stack.push_async_callback(dispatcher.stop)

        notifications = NotificationSettingsService(db)
        github_client = GitHubClient(token=settings.github_token)
        stack.push_async_callback(github_client.aclose)

        github = GitHubTracker(
            db,
            github_client,
            dispatcher,
            notifications,
            timezone_name=settings.scheduler_timezone,
        )
        report = ReportService(
            db,
            dispatcher,
            notifications,
            timezone_name=settings.scheduler_timezone,
            outbound_max_attempts=settings.outbound_max_attempts,
        )
        scheduler = SchedulerService(
            db,
            dispatcher,
            timezone_name=settings.scheduler_timezone,
            llm=llm,
        )
        stack.push_async_callback(scheduler.stop)
        scheduler.register_handler("github_check", github.run_scheduled_check)
        scheduler.register_handler("github_digest", github.run_scheduled_digest)
        scheduler.register_handler("daily_report", report.run_scheduled_report)
        for task_type, cron_expression in (
            ("github_check", settings.github_check_cron),
            (
                "github_digest",
                settings.github_digest_cron if await db.fetch_github_digest_targets() else "",
            ),
            ("daily_report", settings.daily_report_cron),
        ):
            await scheduler.sync_system_task(task_type, cron_expression)

        fetcher = WebFetcher(
            max_bytes=settings.max_webpage_bytes,
            connect_timeout=settings.web_fetch_timeout_connect,
            read_timeout=settings.web_fetch_timeout_read,
            cache_ttl=settings.web_cache_ttl_seconds,
            proxy_url=settings.web_proxy_url or None,
        )
        stack.push_async_callback(fetcher.aclose)

        playwright_fetcher = (
            PlaywrightFetcher(
                max_bytes=settings.max_webpage_bytes,
                navigation_timeout=settings.web_fetch_timeout_read,
                proxy_url=settings.web_proxy_url or None,
            )
            if settings.enable_playwright
            else None
        )
        summarizer = WebSummarizer(
            provider=llm,
            chunk_chars=settings.web_summary_chunk_chars,
            max_chunks=settings.web_summary_max_chunks,
        )

        started_at = time.monotonic()
        runtime = Runtime(
            settings=settings,
            db=db,
            personality=personality,
            memory=MemoryService(db),
            notifications=notifications,
            group_feedback=GroupFeedbackService(db),
            scheduler=scheduler,
            github_client=github_client,
            github=github,
            health=HealthService(db, llm, github_client, scheduler, started_at, check_qq=True),
            report=report,
            sessions=SessionManager(db, settings.max_context_messages),
            llm=llm,
            dispatcher=dispatcher,
            permission=PermissionService(settings.admin_ids),
            fetcher=fetcher,
            playwright_fetcher=playwright_fetcher,
            summarizer=summarizer,
            web_semaphore=asyncio.Semaphore(max(1, settings.max_concurrent_web_tasks)),
            agent=AgentHarness(confirmation_ttl_seconds=settings.agent_confirm_ttl_seconds),
        )

        await dispatcher.start()
        await scheduler.start()

        _runtime = runtime
        stack.pop_all()
        return runtime


def get_runtime() -> Runtime:
    if _runtime is None:
        raise RuntimeError("Runtime 尚未初始化（bot 启动时由 init_runtime 创建）")
    return _runtime


async def close_runtime() -> None:
    """Serialize shutdown against initialization and release all owned resources."""
    async with _runtime_lock:
        await _close_runtime_unlocked()


async def _close_runtime_unlocked() -> None:
    """优雅关闭：释放 LLM HTTP 客户端、抓取客户端与数据库连接。"""
    global _runtime
    if _runtime is None:
        return
    rt, _runtime = _runtime, None
    logger = logging.getLogger(__name__)
    try:
        await rt.scheduler.stop()
    except Exception:
        logger.exception("关闭 Scheduler 失败")
    try:
        await rt.dispatcher.stop()
    except Exception:
        logger.exception("关闭主动消息队列失败")
    try:
        await rt.llm.aclose()
    except Exception:
        logger.exception("关闭 LLM 客户端失败")
    try:
        await rt.github_client.aclose()
    except Exception:
        logger.exception("关闭 GitHub 客户端失败")
    try:
        await rt.fetcher.aclose()
    except Exception:
        logger.exception("关闭抓取客户端失败")
    try:
        await rt.db.close()
    except Exception:
        logger.exception("关闭数据库失败")
