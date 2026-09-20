"""Security invariants that must hold even when deployment preflight is skipped."""

import ipaddress
from urllib.parse import urlsplit

from rumi_helper.security.identifiers import is_ascii_decimal_id


class SecurityConfigurationError(ValueError):
    """Runtime configuration would expose credentials or an unauthenticated service."""


def onebot_token_alias_conflict(settings) -> bool:
    """Return True when canonical and legacy OneBot token aliases disagree."""
    canonical = str(settings.onebot_v11_access_token).strip()
    legacy = str(settings.onebot_access_token).strip()
    return bool(canonical and legacy and canonical != legacy)


def validate_onebot_token_aliases(settings) -> None:
    if onebot_token_alias_conflict(settings):
        raise SecurityConfigurationError(
            "ONEBOT_V11_ACCESS_TOKEN 与 ONEBOT_ACCESS_TOKEN 同时配置但值不一致"
        )


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

    if onebot_token_alias_conflict(settings):
        errors.append(
            "ONEBOT_V11_ACCESS_TOKEN 与 ONEBOT_ACCESS_TOKEN 同时配置但值不一致"
        )

    admin_ids = settings.admin_ids
    if any(not is_ascii_decimal_id(value) for value in admin_ids):
        errors.append("ADMIN_QQ_IDS 必须是逗号分隔的 ASCII 数字 QQ 号")
    owner_id = str(settings.owner_id).strip()
    if owner_id and not is_ascii_decimal_id(owner_id):
        errors.append("OWNER_QQ_ID 必须是 ASCII 数字 QQ 号")

    if not str(settings.onebot_token).strip() and not is_loopback_host(host):
        errors.append(
            "非 loopback 监听时必须配置 ONEBOT_V11_ACCESS_TOKEN"
            "（兼容旧 ONEBOT_ACCESS_TOKEN）"
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
