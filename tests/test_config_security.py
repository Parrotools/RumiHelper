import pytest

from rumi_helper.config import Settings
from rumi_helper.security.config import (
    SecurityConfigurationError,
    is_loopback_host,
    validate_runtime_security,
)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "[::1]", "localhost"])
def test_loopback_hosts_are_recognized(host):
    assert is_loopback_host(host) is True


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "bot.local"])
def test_non_loopback_hosts_are_not_treated_as_local(host):
    assert is_loopback_host(host) is False


def test_runtime_security_allows_loopback_without_onebot_token():
    validate_runtime_security(
        Settings(
            _env_file=None,
            host="127.0.0.1",
            onebot_access_token="",
            llm_base_url="http://127.0.0.1:8000/v1",
            llm_api_key="secret",
        )
    )


def test_runtime_security_rejects_public_bind_without_token():
    settings = Settings(
        _env_file=None,
        host="0.0.0.0",
        onebot_access_token="",
    )

    with pytest.raises(SecurityConfigurationError, match="ONEBOT_ACCESS_TOKEN"):
        validate_runtime_security(settings)


def test_runtime_security_rejects_remote_plain_http_with_api_key():
    settings = Settings(
        _env_file=None,
        llm_base_url="http://llm.example.com/v1",
        llm_api_key="secret",
    )

    with pytest.raises(SecurityConfigurationError, match="HTTPS"):
        validate_runtime_security(settings)


@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@llm.example.com/v1",
        "https://llm.example.com/v1?token=secret",
        "https://llm.example.com/v1#fragment",
    ],
)
def test_runtime_security_rejects_sensitive_or_ambiguous_llm_base_urls(url):
    settings = Settings(
        _env_file=None,
        llm_base_url=url,
    )

    with pytest.raises(SecurityConfigurationError):
        validate_runtime_security(settings)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("admin_qq_ids", "１２３"),
        ("admin_qq_ids", "²"),
        ("owner_qq_id", "①"),
    ],
)
def test_runtime_security_rejects_non_ascii_qq_ids(field, value):
    settings = Settings(_env_file=None, **{field: value})

    with pytest.raises(SecurityConfigurationError, match="ASCII"):
        validate_runtime_security(settings)


def test_onebot_token_prefers_canonical_adapter_setting():
    settings = Settings(
        _env_file=None,
        onebot_v11_access_token="canonical",
        onebot_access_token="legacy",
    )
    assert settings.onebot_token == "canonical"


def test_onebot_token_falls_back_to_legacy_project_setting():
    settings = Settings(
        _env_file=None,
        onebot_v11_access_token="",
        onebot_access_token="legacy",
    )
    assert settings.onebot_token == "legacy"


def test_runtime_security_rejects_conflicting_onebot_token_aliases():
    settings = Settings(
        _env_file=None,
        onebot_v11_access_token="canonical-secret",
        onebot_access_token="legacy-secret",
    )

    with pytest.raises(SecurityConfigurationError, match="值不一致") as exc_info:
        validate_runtime_security(settings)

    assert "canonical-secret" not in str(exc_info.value)
    assert "legacy-secret" not in str(exc_info.value)


def test_runtime_security_allows_matching_onebot_token_aliases():
    settings = Settings(
        _env_file=None,
        onebot_v11_access_token="same-secret",
        onebot_access_token="same-secret",
    )

    validate_runtime_security(settings)
