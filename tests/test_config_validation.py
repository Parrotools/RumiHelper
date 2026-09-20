import pytest

from rumi_helper.config import Settings
from rumi_helper.config_validation import (
    RuntimeConfigurationError,
    numeric_limit_error,
    structural_configuration_error,
    validate_runtime_configuration,
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


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("host", "", "HOST"),
        ("port", 0, "PORT"),
        ("port", 65536, "PORT"),
        ("llm_model", "   ", "LLM_MODEL"),
        ("scheduler_timezone", "Not/A-Timezone", "SCHEDULER_TIMEZONE"),
    ],
)
def test_structural_configuration_error_rejects_invalid_runtime_values(
    field,
    value,
    message,
):
    settings = Settings(_env_file=None, **{field: value})

    detail = structural_configuration_error(settings)

    assert detail is not None
    assert message in detail


def test_validate_runtime_configuration_includes_structural_and_numeric_checks():
    with pytest.raises(RuntimeConfigurationError, match="LLM_MODEL"):
        validate_runtime_configuration(Settings(_env_file=None, llm_model=" "))

    with pytest.raises(RuntimeConfigurationError, match="send_rate_limit_per_second"):
        validate_runtime_configuration(
            Settings(_env_file=None, send_rate_limit_per_second=0)
        )
