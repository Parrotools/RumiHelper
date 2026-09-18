"""URL 识别：从中文混排消息中提取 http/https URL，去重并保持顺序。"""

import re
from urllib.parse import urlsplit

URL_PATTERN = re.compile(r"https?://[^\s<>\"'）】\]]+", re.IGNORECASE)

# 中文/英文常见会吸附在 URL 后面的标点
_TRAILING_PUNCT = ".,;:!?。，、；：！？》〉\"'`"


def extract_urls(text: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for m in URL_PATTERN.finditer(text or ""):
        u = m.group(0).rstrip(_TRAILING_PUNCT)
        # 未配对的右括号视为标点
        while u.endswith(")") and "(" not in u:
            u = u[:-1]
        if not u:
            continue
        key = _dedupe_key(u)
        if key not in seen:
            seen.add(key)
            urls.append(u)
    return urls


def _dedupe_key(url: str) -> str:
    """Normalize only URL components whose case is defined as insensitive."""
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        host_for_netloc = f"[{host}]" if ":" in host else host
        port = f":{parsed.port}" if parsed.port is not None else ""
        userinfo = ""
        if parsed.username is not None:
            userinfo = parsed.username
            if parsed.password is not None:
                userinfo += f":{parsed.password}"
            userinfo += "@"
        netloc = f"{userinfo}{host_for_netloc}{port}"
        return parsed._replace(scheme=parsed.scheme.lower(), netloc=netloc).geturl()
    except ValueError:
        # Malformed URLs are rejected later by the SSRF validator; dedupe should
        # not turn parsing into a separate failure path.
        return url


def has_url(text: str) -> bool:
    return bool(URL_PATTERN.search(text or ""))
