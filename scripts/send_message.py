"""通过 OneBot HTTP API（NapCat httpServers）发送消息，无需 Bot 进程在场。

用法：
    python scripts/send_message.py user:123456 "你好"
    python scripts/send_message.py group:789012 "通知"

环境变量：
    ONEBOT_HTTP_URL     NapCat HTTP 服务端地址，如 http://127.0.0.1:3000
    ONEBOT_ACCESS_TOKEN 与 NapCat 一致的 token
"""

import ipaddress
import os
import sys

import httpx


def _is_loopback_host(host: str) -> bool:
    value = str(host).strip().lower().strip("[]")
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _validate_base_url(value: str, *, has_token: bool) -> str:
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL as exc:
        raise ValueError("ONEBOT_HTTP_URL 无法解析") from exc
    if url.scheme not in {"http", "https"} or not url.host:
        raise ValueError("ONEBOT_HTTP_URL 必须是 http(s) URL")
    if url.userinfo:
        raise ValueError("ONEBOT_HTTP_URL 不允许包含用户名或密码")
    if has_token and url.scheme == "http" and not _is_loopback_host(url.host):
        raise ValueError("携带 ONEBOT_ACCESS_TOKEN 的远程地址必须使用 HTTPS")
    return str(url).rstrip("/")


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2

    target, message = sys.argv[1], sys.argv[2]
    type_, sep, target_id = target.partition(":")
    type_ = type_.strip().lower()
    if not sep or type_ not in ("user", "group") or not target_id.isdigit():
        print("目标格式应为 user:QQ号 或 group:群号")
        return 2

    token = os.environ.get("ONEBOT_ACCESS_TOKEN", "")
    try:
        base_url = _validate_base_url(
            os.environ.get("ONEBOT_HTTP_URL", "http://127.0.0.1:3000"),
            has_token=bool(token),
        )
    except ValueError as exc:
        print(f"配置错误：{exc}")
        return 2

    if type_ == "user":
        action, params = "send_private_msg", {"user_id": int(target_id), "message": message}
    else:
        action, params = "send_group_msg", {"group_id": int(target_id), "message": message}

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        resp = httpx.post(f"{base_url}/{action}", json=params, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as e:
        print(f"发送失败：{type(e).__name__}")
        return 1
    except ValueError:
        print("发送失败：OneBot 返回了无效 JSON")
        return 1

    if not isinstance(data, dict):
        print("发送失败：OneBot 返回格式无效")
        return 1
    if data.get("status") in ("ok", "async") or data.get("retcode") == 0:
        print(f"发送成功 message_id={data.get('data', {}).get('message_id')}")
        return 0
    print(
        "发送失败："
        f"retcode={data.get('retcode', 'unknown')} "
        f"status={data.get('status', 'unknown')}"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
