import ipaddress

import pytest

from app.security.ssrf import (
    SSRFBlockedError,
    assert_url_safe,
    check_ips,
    is_ip_allowed,
    validate_url,
)


def _blocked(ips: list[str]) -> None:
    for s in ips:
        assert not is_ip_allowed(ipaddress.ip_address(s)), f"{s} 应被拒绝"


def test_private_and_reserved_ips_blocked():
    _blocked([
        "127.0.0.1",
        "127.8.8.8",
        "10.0.0.1",
        "192.168.1.1",
        "172.16.0.1",
        "172.31.255.255",
        "0.0.0.0",
        "169.254.169.254",  # 云 metadata
        "100.64.0.1",       # CGNAT
        "224.0.0.1",        # 组播
        "240.0.0.1",        # 保留
    ])


def test_ipv6_blocked():
    _blocked(["::1", "fe80::1", "fc00::1", "::", "ff02::1", "::ffff:127.0.0.1", "::ffff:192.168.1.1"])


def test_public_ips_allowed():
    for s in ["93.184.216.34", "8.8.8.8", "1.1.1.1", "2606:4700::1111"]:
        assert is_ip_allowed(ipaddress.ip_address(s)), f"{s} 应放行"


def test_validate_url_scheme_rejected():
    for url in [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "data:text/html;base64,xxx",
        "javascript:alert(1)",
        "gopher://example.com",
    ]:
        with pytest.raises(SSRFBlockedError):
            validate_url(url)


def test_validate_url_valid():
    parsed = validate_url("https://example.com/a?b=1")
    assert parsed.host == "example.com"


async def test_assert_url_safe_blocks_loopback_literal():
    # 数值 IP 字面量，不需要真实 DNS
    with pytest.raises(SSRFBlockedError):
        await assert_url_safe("http://127.0.0.1:8080")


async def test_assert_url_safe_blocks_localhost():
    with pytest.raises(SSRFBlockedError):
        await assert_url_safe("http://localhost/secret")


async def test_assert_url_safe_dns_resolves_to_private(monkeypatch):
    # 模拟 nip.io 类攻击：域名解析到内网 IP
    from app.security import ssrf

    async def fake_resolve(host):
        return [ipaddress.ip_address("192.168.1.1")]

    monkeypatch.setattr(ssrf, "resolve_host", fake_resolve)
    with pytest.raises(SSRFBlockedError):
        await assert_url_safe("http://example.com/")


async def test_assert_url_safe_public_ok(monkeypatch):
    from app.security import ssrf

    async def fake_resolve(host):
        return [ipaddress.ip_address("93.184.216.34")]

    monkeypatch.setattr(ssrf, "resolve_host", fake_resolve)
    assert (await assert_url_safe("https://example.com/")).host == "example.com"


def test_check_ips_raises():
    with pytest.raises(SSRFBlockedError):
        check_ips([ipaddress.ip_address("8.8.8.8"), ipaddress.ip_address("127.0.0.1")])


def test_validate_url_rejects_userinfo_credentials():
    with pytest.raises(SSRFBlockedError):
        validate_url("https://user:password@example.com/private")
