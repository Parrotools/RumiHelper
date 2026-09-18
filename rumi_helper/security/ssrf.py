"""SSRF 防护。

所有对外抓取的 URL 必须经过本模块校验：
1. 只允许 http/https scheme；
2. 域名必须先 DNS 解析，解析出的每个 IP 都要检查（不能只用字符串判断 host）；
3. 重定向的每一跳都要重新做 1-2（由 fetcher 手动控制 redirect 实现）。
"""

import asyncio
import ipaddress

import httpx

ALLOWED_SCHEMES = frozenset({"http", "https"})


class SSRFBlockedError(Exception):
    """目标 URL 解析到被禁止的地址（内网 / 保留 / link-local 等），或 scheme 不允许。"""


def is_ip_allowed(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # IPv6 内嵌 IPv4（如 ::ffff:192.168.1.1）按映射的 IPv4 判断
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not (
        ip.is_unspecified
        or ip.is_loopback
        or ip.is_link_local  # 含 169.254.169.254 云 metadata
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_private  # RFC1918、CGNAT 等
        or not ip.is_global  # 兜底：只放行公网全局地址
    )


def validate_url(url: str) -> httpx.URL:
    try:
        parsed = httpx.URL(url)
    except Exception as e:
        raise SSRFBlockedError("无法解析的 URL") from e
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise SSRFBlockedError(f"不允许的协议：{parsed.scheme or '(空)'}，只支持 http/https")
    if not parsed.host:
        raise SSRFBlockedError("URL 缺少主机名")
    if parsed.userinfo:
        raise SSRFBlockedError("URL 不允许包含用户名或密码")
    return parsed


async def resolve_host(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None)
    except OSError as e:
        raise SSRFBlockedError(f"域名解析失败：{host}") from e
    return [ipaddress.ip_address(sockaddr[0]) for *_x, sockaddr in infos]


def check_ips(ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address]) -> None:
    for ip in ips:
        if not is_ip_allowed(ip):
            raise SSRFBlockedError(f"目标解析到被禁止的地址：{ip}")


async def resolve_url_safe(
    url: str,
) -> tuple[httpx.URL, list[ipaddress.IPv4Address | ipaddress.IPv6Address]]:
    """返回已完成 scheme/DNS/IP 校验的 URL 和解析结果。"""
    parsed = validate_url(url)
    ips = await resolve_host(parsed.host)
    if not ips:
        raise SSRFBlockedError(f"域名没有任何解析结果：{parsed.host}")
    check_ips(ips)
    return parsed, ips


async def assert_url_safe(url: str) -> httpx.URL:
    """scheme 校验 + DNS 解析 + IP 校验。抓取每一跳前都必须调用。"""
    parsed, _ips = await resolve_url_safe(url)
    return parsed
