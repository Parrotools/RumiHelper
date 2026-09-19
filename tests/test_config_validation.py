import pytest

from rumi_helper.config import Settings
from rumi_helper.config_validation import (
    RuntimeConfigurationError,
    numeric_limit_error,
    validate_runtime_limits,
)


def test_numeric_limit_error_is_secret_free_and_names_invalid_field():
    settings = Settings(
        _env_file=None,
        send_rate_limit_per_second=0,
        llm_api_key="super-secret-api-key",
    )

    detail = numeric_limit_error(settings)

    assert detail is not None
    assert "send_rate_limit_per_second" in detail
    assert "super-secret-api-key" not in detail


def test_validate_runtime_limits_allows_documented_zero_disable_values():
    settings = Settings(
        _env_file=None,
        web_cache_ttl_seconds=0,
        outbound_terminal_history_limit=0,
        send_log_history_limit=0,
    )

    validate_runtime_limits(settings)


def test_validate_runtime_limits_rejects_inverted_reply_delay_range():
    settings = Settings(
        _env_file=None,
        non_llm_reply_delay_min_seconds=5,
        non_llm_reply_delay_max_seconds=1,
    )

    with pytest.raises(RuntimeConfigurationError, match="MIN_SECONDS"):
        validate_runtime_limits(settings)
