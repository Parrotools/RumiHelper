"""Shared runtime configuration validation beyond Pydantic type parsing."""

from __future__ import annotations

import math
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class RuntimeConfigurationError(ValueError):
    """A typed configuration value violates a runtime invariant."""


def numeric_limit_error(settings: Any) -> str | None:
    """Return a secret-free description of the first numeric invariant failure."""
    positive = {
        "llm_timeout": settings.llm_timeout,
        "max_context_messages": settings.max_context_messages,
        "max_context_chars": settings.max_context_chars,
        "max_webpage_bytes": settings.max_webpage_bytes,
        "web_fetch_timeout_connect": settings.web_fetch_timeout_connect,
        "web_fetch_timeout_read": settings.web_fetch_timeout_read,
        "web_summary_chunk_chars": settings.web_summary_chunk_chars,
        "web_summary_max_chunks": settings.web_summary_max_chunks,
        "max_broadcast_recipients": settings.max_broadcast_recipients,
        "send_rate_limit_per_second": settings.send_rate_limit_per_second,
        "interactive_send_rate_limit_per_second": (
            settings.interactive_send_rate_limit_per_second
        ),
        "outbound_max_attempts": settings.outbound_max_attempts,
        "outbound_retry_delay_seconds": settings.outbound_retry_delay_seconds,
        "outbound_account_pause_seconds": settings.outbound_account_pause_seconds,
        "outbound_rate_limit_pause_seconds": settings.outbound_rate_limit_pause_seconds,
        "outbound_queue_poll_seconds": settings.outbound_queue_poll_seconds,
        "outbound_lease_seconds": settings.outbound_lease_seconds,
        "max_concurrent_web_tasks": settings.max_concurrent_web_tasks,
        "max_concurrent_llm_tasks": settings.max_concurrent_llm_tasks,
        "agent_confirm_ttl_seconds": settings.agent_confirm_ttl_seconds,
        "broadcast_confirm_ttl_seconds": settings.broadcast_confirm_ttl_seconds,
        "max_active_reminders_per_user": settings.max_active_reminders_per_user,
        "max_github_repositories_per_user": settings.max_github_repositories_per_user,
        "non_llm_reply_delay_chars_per_second": (
            settings.non_llm_reply_delay_chars_per_second
        ),
    }
    nonnegative = {
        "llm_temperature": settings.llm_temperature,
        "web_cache_ttl_seconds": settings.web_cache_ttl_seconds,
        "outbound_terminal_history_limit": settings.outbound_terminal_history_limit,
        "send_log_history_limit": settings.send_log_history_limit,
        "scheduled_task_history_limit": settings.scheduled_task_history_limit,
        "session_retention_days": settings.session_retention_days,
        "non_llm_reply_delay_min_seconds": settings.non_llm_reply_delay_min_seconds,
        "non_llm_reply_delay_max_seconds": settings.non_llm_reply_delay_max_seconds,
    }
    if settings.non_llm_reply_delay_seconds is not None:
        nonnegative["non_llm_reply_delay_seconds"] = (
            settings.non_llm_reply_delay_seconds
        )

    nonfinite = sorted(
        name
        for name, value in {**positive, **nonnegative}.items()
        if not math.isfinite(float(value))
    )
    if nonfinite:
        return f"必须是有限数值：{', '.join(nonfinite)}"

    invalid_positive = sorted(
        name for name, value in positive.items() if float(value) <= 0
    )
    if invalid_positive:
        return f"必须大于 0：{', '.join(invalid_positive)}"

    invalid_nonnegative = sorted(
        name for name, value in nonnegative.items() if float(value) < 0
    )
    if invalid_nonnegative:
        return f"不能小于 0：{', '.join(invalid_nonnegative)}"

    if (
        float(settings.non_llm_reply_delay_min_seconds)
        > float(settings.non_llm_reply_delay_max_seconds)
    ):
        return "NON_LLM_REPLY_DELAY_MIN_SECONDS 不能大于 MAX_SECONDS"
    return None


def structural_configuration_error(settings: Any) -> str | None:
    """Return a secret-free description of a non-numeric runtime invariant failure."""
    if not str(settings.host).strip():
        return "HOST 不能为空"
    if not 1 <= int(settings.port) <= 65535:
        return "PORT 必须在 1..65535"
    if not str(settings.llm_model).strip():
        return "LLM_MODEL 不能为空"
    try:
        ZoneInfo(str(settings.scheduler_timezone))
    except (ZoneInfoNotFoundError, ValueError):
        return "SCHEDULER_TIMEZONE 不存在"
    return None


def validate_runtime_limits(settings: Any) -> None:
    """Raise before resources are created when numeric runtime invariants are invalid."""
    error = numeric_limit_error(settings)
    if error is not None:
        raise RuntimeConfigurationError(error)


def validate_runtime_configuration(settings: Any) -> None:
    """Validate all non-secret runtime invariants before creating resources."""
    error = structural_configuration_error(settings) or numeric_limit_error(settings)
    if error is not None:
        raise RuntimeConfigurationError(error)
