import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "send_message_script",
    Path(__file__).parents[1] / "scripts" / "send_message.py",
)
send_message = importlib.util.module_from_spec(spec)
spec.loader.exec_module(send_message)


class FakeResponse:
    def __init__(self, payload=None, *, invalid_json: bool = False):
        self._payload = payload
        self._invalid_json = invalid_json

    def raise_for_status(self) -> None:
        return None

    def json(self):
        if self._invalid_json:
            raise ValueError("bad json")
        return self._payload


def test_validate_base_url_allows_loopback_http_with_token():
    assert (
        send_message._validate_base_url(
            "http://127.0.0.1:3000",
            has_token=True,
        )
        == "http://127.0.0.1:3000"
    )


def test_validate_base_url_rejects_remote_http_with_token():
    with pytest.raises(ValueError, match="HTTPS"):
        send_message._validate_base_url(
            "http://onebot.example.com:3000",
            has_token=True,
        )


def test_validate_base_url_rejects_userinfo():
    with pytest.raises(ValueError, match="用户名或密码"):
        send_message._validate_base_url(
            "https://user:password@onebot.example.com",
            has_token=False,
        )


def test_main_rejects_insecure_remote_token_before_request(monkeypatch, capsys):
    called = False

    def fake_post(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("request must not be sent")

    monkeypatch.setattr(send_message.httpx, "post", fake_post)
    monkeypatch.setattr(
        send_message.sys,
        "argv",
        ["send_message.py", "user:123", "hello"],
    )
    monkeypatch.setenv("ONEBOT_HTTP_URL", "http://onebot.example.com:3000")
    monkeypatch.setenv("ONEBOT_ACCESS_TOKEN", "secret")

    assert send_message.main() == 2
    assert called is False
    assert "必须使用 HTTPS" in capsys.readouterr().out


def test_main_sends_auth_and_message_to_secure_remote(monkeypatch, capsys):
    captured = {}

    def fake_post(url, *, json, headers, timeout):
        captured.update(url=url, json=json, headers=headers, timeout=timeout)
        return FakeResponse({"status": "ok", "retcode": 0, "data": {"message_id": 7}})

    monkeypatch.setattr(send_message.httpx, "post", fake_post)
    monkeypatch.setattr(
        send_message.sys,
        "argv",
        ["send_message.py", "group:456", "hello"],
    )
    monkeypatch.setenv("ONEBOT_HTTP_URL", "https://onebot.example.com/api")
    monkeypatch.setenv("ONEBOT_ACCESS_TOKEN", "secret")

    assert send_message.main() == 0
    assert captured["url"] == "https://onebot.example.com/api/send_group_msg"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["json"] == {
        "group_id": 456,
        "message": "hello",
        "auto_escape": True,
    }
    assert "message_id=7" in capsys.readouterr().out


def test_main_marks_cq_code_payload_as_plain_text(monkeypatch):
    captured = {}

    def fake_post(url, *, json, headers, timeout):
        captured.update(url=url, json=json, headers=headers, timeout=timeout)
        return FakeResponse({"status": "ok", "retcode": 0, "data": {"message_id": 8}})

    payload = "[CQ:at,qq=all]"
    monkeypatch.setattr(send_message.httpx, "post", fake_post)
    monkeypatch.setattr(
        send_message.sys,
        "argv",
        ["send_message.py", "group:456", payload],
    )
    monkeypatch.setenv("ONEBOT_HTTP_URL", "http://127.0.0.1:3000")
    monkeypatch.setenv("ONEBOT_ACCESS_TOKEN", "")

    assert send_message.main() == 0
    assert captured["json"]["message"] == payload
    assert captured["json"]["auto_escape"] is True


def test_main_handles_invalid_json_without_traceback(monkeypatch, capsys):
    monkeypatch.setattr(
        send_message.httpx,
        "post",
        lambda *args, **kwargs: FakeResponse(invalid_json=True),
    )
    monkeypatch.setattr(
        send_message.sys,
        "argv",
        ["send_message.py", "user:123", "hello"],
    )
    monkeypatch.setenv("ONEBOT_HTTP_URL", "http://127.0.0.1:3000")
    monkeypatch.setenv("ONEBOT_ACCESS_TOKEN", "")

    assert send_message.main() == 1
    assert "无效 JSON" in capsys.readouterr().out


def test_main_accepts_matching_onebot_token_aliases(monkeypatch):
    captured = {}

    def fake_post(url, *, json, headers, timeout):
        captured["authorization"] = headers.get("Authorization")
        return FakeResponse({"status": "ok", "retcode": 0, "data": {"message_id": 9}})

    monkeypatch.setattr(send_message.httpx, "post", fake_post)
    monkeypatch.setattr(
        send_message.sys,
        "argv",
        ["send_message.py", "user:123", "hello"],
    )
    monkeypatch.setenv("ONEBOT_HTTP_URL", "https://onebot.example.com/api")
    monkeypatch.setenv("ONEBOT_ACCESS_TOKEN", "same")
    monkeypatch.setenv("ONEBOT_V11_ACCESS_TOKEN", "same")

    assert send_message.main() == 0
    assert captured["authorization"] == "Bearer same"


def test_main_rejects_conflicting_onebot_token_aliases_without_request(
    monkeypatch,
    capsys,
):
    called = False
    canonical = "canonical-secret-do-not-print"
    legacy = "legacy-secret-do-not-print"

    def fake_post(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("request must not be sent")

    monkeypatch.setattr(send_message.httpx, "post", fake_post)
    monkeypatch.setattr(
        send_message.sys,
        "argv",
        ["send_message.py", "user:123", "hello"],
    )
    monkeypatch.setenv("ONEBOT_HTTP_URL", "https://onebot.example.com/api")
    monkeypatch.setenv("ONEBOT_ACCESS_TOKEN", legacy)
    monkeypatch.setenv("ONEBOT_V11_ACCESS_TOKEN", canonical)

    assert send_message.main() == 2
    output = capsys.readouterr().out
    assert called is False
    assert "值不一致" in output
    assert canonical not in output
    assert legacy not in output


@pytest.mark.parametrize("target", ["user:0", "group:000", "user:１２３", "channel:123"])
def test_main_rejects_invalid_qq_targets_before_request(monkeypatch, target):
    called = False

    def fake_post(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("request must not be sent")

    monkeypatch.setattr(send_message.httpx, "post", fake_post)
    monkeypatch.setattr(
        send_message.sys,
        "argv",
        ["send_message.py", target, "hello"],
    )

    assert send_message.main() == 2
    assert called is False
