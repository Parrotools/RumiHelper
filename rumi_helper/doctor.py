"""Offline deployment preflight checks.

This module intentionally avoids network calls and never prints credential values.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from apscheduler.triggers.cron import CronTrigger

from rumi_helper.config import Settings
from rumi_helper.personality.manager import PersonalityConfigError, PersonalityManager
from rumi_helper.promptlib import load_prompt

CheckStatus = Literal["PASS", "WARN", "FAIL"]
_REQUIRED_PROMPTS = ("agent.txt", "chat.txt", "webpage_summary.txt")


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: CheckStatus
    detail: str


def run_checks(settings: Settings) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []

    checks.append(_check_bind(settings))
    checks.append(_check_bind_availability(settings))
    checks.append(_check_llm_url(settings))
    checks.extend(_check_credentials(settings))
    checks.extend(_check_transport_security(settings))
    checks.append(_check_personality(settings))
    checks.append(_check_prompts())
    checks.extend(_check_scheduler(settings))
    checks.append(_check_database_path(settings))
    checks.extend(_check_numeric_limits(settings))
    checks.append(_check_log_level(settings))
    return checks


def _check_bind(settings: Settings) -> DoctorCheck:
    if not str(settings.host).strip():
        return DoctorCheck("bind", "FAIL", "HOST 不能为空")
    if not 1 <= int(settings.port) <= 65535:
        return DoctorCheck("bind", "FAIL", "PORT 必须在 1..65535")
    return DoctorCheck("bind", "PASS", f"{settings.host}:{settings.port}")


def _check_bind_availability(settings: Settings) -> DoctorCheck:
    host = str(settings.host).strip()
    port = int(settings.port)
    if not host or not 1 <= port <= 65535:
        return DoctorCheck("bind_available", "FAIL", "监听地址无效，无法检查端口")

    try:
        infos = socket.getaddrinfo(
            host,
            port,
            type=socket.SOCK_STREAM,
            flags=socket.AI_PASSIVE,
        )
    except OSError:
        return DoctorCheck("bind_available", "FAIL", "HOST 无法解析为本机监听地址")

    errors: list[OSError] = []
    for family, socktype, proto, _canonname, sockaddr in infos:
        sock = socket.socket(family, socktype, proto)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(sockaddr)
        except OSError as exc:
            errors.append(exc)
        else:
            return DoctorCheck("bind_available", "PASS", f"{host}:{port} 可绑定")
        finally:
            sock.close()

    detail = type(errors[-1]).__name__ if errors else "OSError"
    return DoctorCheck(
        "bind_available",
        "FAIL",
        f"{host}:{port} 无法绑定（{detail}，可能已被占用或无权限）",
    )


def _check_llm_url(settings: Settings) -> DoctorCheck:
    try:
        url = httpx.URL(settings.llm_base_url)
    except httpx.InvalidURL:
        return DoctorCheck("llm_url", "FAIL", "LLM_BASE_URL 无法解析")
    if url.scheme not in {"http", "https"} or not url.host:
        return DoctorCheck("llm_url", "FAIL", "LLM_BASE_URL 必须是 http(s) URL")
    return DoctorCheck("llm_url", "PASS", f"{url.scheme}://{url.host}")


def _check_credentials(settings: Settings) -> list[DoctorCheck]:
    checks = [
        DoctorCheck(
            "onebot_token",
            "PASS" if settings.onebot_access_token.strip() else "WARN",
            "已配置" if settings.onebot_access_token.strip() else "未配置；仅在 NapCat 同样关闭鉴权时可用",
        ),
        DoctorCheck(
            "llm_api_key",
            "PASS" if settings.llm_api_key.strip() else "WARN",
            "已配置" if settings.llm_api_key.strip() else "未配置；仅适用于无需 API Key 的 provider",
        ),
    ]

    admin_ids = settings.admin_ids
    if any(not value.isdigit() for value in admin_ids):
        checks.append(DoctorCheck("admin_ids", "FAIL", "ADMIN_QQ_IDS 必须是逗号分隔的数字 QQ 号"))
    elif not admin_ids:
        checks.append(DoctorCheck("admin_ids", "WARN", "未配置管理员；管理员命令将不可用"))
    else:
        checks.append(DoctorCheck("admin_ids", "PASS", f"已配置 {len(admin_ids)} 个管理员"))

    owner = settings.owner_id
    if owner and not owner.isdigit():
        checks.append(DoctorCheck("owner_id", "FAIL", "OWNER_QQ_ID 必须是数字 QQ 号"))
    elif owner:
        checks.append(DoctorCheck("owner_id", "PASS", "已配置"))
    else:
        checks.append(DoctorCheck("owner_id", "WARN", "未解析到唯一主人；owner 身份功能将关闭"))
    return checks


def _is_loopback_host(host: str) -> bool:
    value = str(host).strip().lower().strip("[]")
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _check_transport_security(settings: Settings) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []

    if settings.onebot_access_token.strip():
        checks.append(DoctorCheck("onebot_exposure", "PASS", "OneBot 鉴权已启用"))
    elif _is_loopback_host(settings.host):
        checks.append(
            DoctorCheck(
                "onebot_exposure",
                "WARN",
                "仅本机监听但未配置 OneBot token",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                "onebot_exposure",
                "FAIL",
                "非 loopback 监听时必须配置 ONEBOT_ACCESS_TOKEN",
            )
        )

    try:
        llm_url = httpx.URL(settings.llm_base_url)
    except httpx.InvalidURL:
        return checks
    if (
        settings.llm_api_key.strip()
        and llm_url.scheme == "http"
        and llm_url.host
        and not _is_loopback_host(llm_url.host)
    ):
        checks.append(
            DoctorCheck(
                "llm_transport",
                "FAIL",
                "远程 LLM 使用 API Key 时必须使用 HTTPS",
            )
        )
    else:
        checks.append(DoctorCheck("llm_transport", "PASS", "传输配置可接受"))
    return checks


def _check_personality(settings: Settings) -> DoctorCheck:
    try:
        manager = PersonalityManager(settings.personality_file)
    except PersonalityConfigError:
        return DoctorCheck("personality", "FAIL", "人格文件缺失或格式无效")
    return DoctorCheck("personality", "PASS", manager.personality.name)


def _check_prompts() -> DoctorCheck:
    try:
        for name in _REQUIRED_PROMPTS:
            if not load_prompt(name).strip():
                return DoctorCheck("prompts", "FAIL", f"{name} 为空")
    except OSError:
        return DoctorCheck("prompts", "FAIL", "运行时 prompt 文件缺失")
    return DoctorCheck("prompts", "PASS", f"{len(_REQUIRED_PROMPTS)} 个 prompt 可读")


def _check_scheduler(settings: Settings) -> list[DoctorCheck]:
    try:
        timezone = ZoneInfo(settings.scheduler_timezone)
    except ZoneInfoNotFoundError:
        return [DoctorCheck("timezone", "FAIL", "SCHEDULER_TIMEZONE 不存在")]

    checks = [DoctorCheck("timezone", "PASS", settings.scheduler_timezone)]
    for name, expression in (
        ("github_check_cron", settings.github_check_cron),
        ("github_digest_cron", settings.github_digest_cron),
        ("daily_report_cron", settings.daily_report_cron),
    ):
        expression = expression.strip()
        if not expression:
            checks.append(DoctorCheck(name, "PASS", "disabled"))
            continue
        try:
            CronTrigger.from_crontab(expression, timezone=timezone)
        except ValueError:
            checks.append(DoctorCheck(name, "FAIL", "cron 必须是合法的 5 段表达式"))
        else:
            checks.append(DoctorCheck(name, "PASS", expression))
    return checks


def _check_database_path(settings: Settings) -> DoctorCheck:
    path = Path(settings.database_path).expanduser()
    if path.exists():
        if not path.is_file():
            return DoctorCheck("database_path", "FAIL", "DATABASE_PATH 已存在但不是文件")
        if not os.access(path, os.R_OK | os.W_OK):
            return DoctorCheck("database_path", "FAIL", "数据库文件不可读写")
        return DoctorCheck("database_path", "PASS", str(path))

    parent = path.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    if not parent.exists() or not parent.is_dir() or not os.access(parent, os.W_OK):
        return DoctorCheck("database_path", "FAIL", "数据库目录不可写")
    return DoctorCheck("database_path", "PASS", f"{path}（将在首次启动创建）")


def _check_numeric_limits(settings: Settings) -> list[DoctorCheck]:
    values = {
        "llm_timeout": settings.llm_timeout,
        "max_webpage_bytes": settings.max_webpage_bytes,
        "web_fetch_timeout_connect": settings.web_fetch_timeout_connect,
        "web_fetch_timeout_read": settings.web_fetch_timeout_read,
        "web_summary_chunk_chars": settings.web_summary_chunk_chars,
        "web_summary_max_chunks": settings.web_summary_max_chunks,
        "max_broadcast_recipients": settings.max_broadcast_recipients,
        "outbound_max_attempts": settings.outbound_max_attempts,
        "outbound_lease_seconds": settings.outbound_lease_seconds,
        "max_concurrent_web_tasks": settings.max_concurrent_web_tasks,
        "max_concurrent_llm_tasks": settings.max_concurrent_llm_tasks,
        "agent_confirm_ttl_seconds": settings.agent_confirm_ttl_seconds,
        "broadcast_confirm_ttl_seconds": settings.broadcast_confirm_ttl_seconds,
    }
    invalid = sorted(name for name, value in values.items() if float(value) <= 0)
    if invalid:
        return [DoctorCheck("numeric_limits", "FAIL", f"必须大于 0：{', '.join(invalid)}")]
    return [DoctorCheck("numeric_limits", "PASS", f"{len(values)} 项范围正常")]


def _check_log_level(settings: Settings) -> DoctorCheck:
    value = settings.log_level.strip().upper()
    if value not in logging.getLevelNamesMapping():
        return DoctorCheck("log_level", "WARN", "未知 LOG_LEVEL；运行时会回退 INFO")
    return DoctorCheck("log_level", "PASS", value)


def main() -> int:
    try:
        settings = Settings()
    except Exception as exc:  # noqa: BLE001 - never echo validation values/secrets
        print(f"CHECK settings FAIL {type(exc).__name__}")
        print("RESULT fail")
        return 1

    checks = run_checks(settings)
    for check in checks:
        print(f"CHECK {check.name} {check.status} {check.detail}")
    failed = any(check.status == "FAIL" for check in checks)
    print("RESULT fail" if failed else "RESULT pass")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
