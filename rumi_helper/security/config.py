"""Security invariants that must hold even when deployment preflight is skipped."""

import ipaddress
from urllib.parse import urlsplit


class SecurityConfigurationError(ValueError):
    """Runtime configuration would expose credentials or an unauthenticated service."""


def is_loopback_host(host: str) -> bool:
    value = str(host).strip().lower().strip("[]")
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def validate_runtime_security(settings) -> None:
    errors: list[str] = []

    host = str(settings.host).strip()
    if not str(settings.onebot_access_token).strip() and not is_loopback_host(host):
        errors.append(
            "非 loopback 监听时必须配置 ONEBOT_ACCESS_TOKEN"
        )

    raw_llm_url = str(settings.llm_base_url).strip()
    try:
        parsed = urlsplit(raw_llm_url)
        port = parsed.port
        del port
    except ValueError as exc:
        raise SecurityConfigurationError("LLM_BASE_URL 无法解析") from exc

    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        errors.append("LLM_BASE_URL 必须是 http(s) URL")
    else:
        if parsed.username is not None or parsed.password is not None:
            errors.append("LLM_BASE_URL 不允许包含用户名或密码")
        if parsed.query or parsed.fragment:
            errors.append("LLM_BASE_URL 不允许包含 query 或 fragment")
        if (
            str(settings.llm_api_key).strip()
            and parsed.scheme == "http"
            and not is_loopback_host(parsed.hostname)
        ):
            errors.append("远程 LLM 使用 API Key 时必须使用 HTTPS")

    if errors:
        raise SecurityConfigurationError("；".join(errors))
