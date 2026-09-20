"""集中配置：全部来自环境变量 / .env，禁止硬编码任何密钥、QQ 号、地址。"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # OneBot / NoneBot
    onebot_access_token: str = ""
    host: str = "127.0.0.1"
    port: int = 8080

    # LLM（OpenAI Compatible）
    llm_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    llm_api_key: str = ""
    llm_model: str = "glm-4-flash"
    llm_timeout: float = 60.0
    llm_temperature: float = 0.9
    # 不调用 LLM 的交互回复动态延迟：按文本量增长，并在最小/最大值之间限制
    non_llm_reply_delay_min_seconds: float = 2.0
    non_llm_reply_delay_max_seconds: float = 6.0
    non_llm_reply_delay_chars_per_second: float = 35.0
    # 兼容旧配置：如果设置了 NON_LLM_REPLY_DELAY_SECONDS，将其作为最大延迟；设为 0 关闭
    non_llm_reply_delay_seconds: float | None = None

    # 人格
    personality_file: str = "rumi_helper/personality/rumi.yaml"
    owner_qq_id: str = ""
    owner_name: str = "Parrotools"

    # 会话
    max_context_messages: int = 20
    max_context_chars: int = 16000
    session_retention_days: int = 90
    group_shared_context: bool = False

    # 网页
    url_auto_summary_mode: Literal["off", "mentioned", "all"] = "mentioned"
    max_webpage_bytes: int = 4_194_304
    web_fetch_timeout_connect: float = 10.0
    web_fetch_timeout_read: float = 30.0
    # SSRF-safe web proxy. Only SOCKS5 is accepted because HTTP CONNECT
    # cannot preserve the original TLS SNI when the request URL is pinned to an IP.
    web_proxy_url: str = ""
    web_summary_chunk_chars: int = 4000
    web_summary_max_chunks: int = 8
    enable_playwright: bool = False
    # 同一 URL 抓取结果缓存秒数（0 关闭）
    web_cache_ttl_seconds: float = 600.0

    # 权限与群发
    admin_qq_ids: str = ""
    max_broadcast_recipients: int = 20
    # 所有主动发送（direct/broadcast/outbound queue）在同一 SQLite 数据库实例上共享速率上限
    send_rate_limit_per_second: float = 1.0
    # 所有交互回复共用的最小发送速率，避免多个并发会话瞬时集中发送
    interactive_send_rate_limit_per_second: float = 0.5
    outbound_max_attempts: int = 3
    outbound_retry_delay_seconds: float = 30.0
    # 账号掉线或触发接口限流时，暂停主动投递，避免继续撞接口
    outbound_account_pause_seconds: float = 1800.0
    outbound_rate_limit_pause_seconds: float = 60.0
    outbound_queue_poll_seconds: float = 1.0
    outbound_lease_seconds: float = 60.0
    outbound_terminal_history_limit: int = 5000
    send_log_history_limit: int = 5000
    broadcast_require_confirm: bool = True
    broadcast_confirm_ttl_seconds: int = 300

    # 并发
    max_concurrent_web_tasks: int = 3
    max_concurrent_llm_tasks: int = 5

    # 自然语言工具调用
    agent_tool_calling_enabled: bool = True
    agent_confirm_ttl_seconds: int = 300

    # 定时任务
    scheduler_timezone: str = "Asia/Shanghai"
    max_active_reminders_per_user: int = 50
    scheduled_task_history_limit: int = 5000
    max_github_repositories_per_user: int = 50
    github_token: str = ""
    github_check_cron: str = "0 * * * *"
    github_digest_cron: str = "0 6,18 * * *"
    daily_report_cron: str = "0 23 * * *"

    # 数据 / 日志
    database_path: str = "data/bot.db"
    log_level: str = "INFO"

    @property
    def admin_ids(self) -> frozenset[str]:
        return frozenset(x.strip() for x in self.admin_qq_ids.split(",") if x.strip())

    @property
    def owner_id(self) -> str:
        explicit_owner = self.owner_qq_id.strip()
        if explicit_owner:
            return explicit_owner
        # 兼容只有一个管理员的既有部署；多个管理员时必须显式配置 OWNER_QQ_ID。
        admin_ids = self.admin_ids
        return next(iter(admin_ids), "") if len(admin_ids) == 1 else ""

@lru_cache
def get_settings() -> Settings:
    return Settings()
