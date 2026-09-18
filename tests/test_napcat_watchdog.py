import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "napcat_watchdog", Path(__file__).parents[1] / "ops/napcat/napcat_watchdog.py"
)
watchdog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watchdog)

NOW = 200_000.0


def decide(state=None, **kwargs):
    options = {"now": NOW, "running": True, "started": NOW - 600, "online": False,
               "error": "none", "has_password": True}
    options.update(kwargs)
    return watchdog.decide(state or {}, **options)


def test_recovered_login_overrides_old_error_and_manual_lock():
    state, action = decide({"manual": True}, online=True, error="verification_required")
    assert action == "wait"
    assert state["status"] == "online"
    assert state["manual"] is False


@pytest.mark.parametrize("online,running,status", [
    (None, True, "probe_unavailable"), (False, False, "service_stopped"),
])
def test_observation_failure_or_stopped_service_does_not_restart(online, running, status):
    state, action = decide({"first_offline": NOW - 3600}, online=online, running=running)
    assert action == "wait"
    assert state["status"] == status


def test_expired_session_tries_password_once_even_if_process_dies_during_request():
    state, action = decide(error="session_expired")
    assert action == "password"
    state, action = decide(state, error="session_expired", now=NOW + 60)
    assert action == "wait"
    assert state["manual"] is True


def test_password_daily_budget_survives_successful_login():
    state, _ = decide(error="session_expired")
    state, _ = decide(state, online=True)
    state, action = decide(state, error="session_expired", now=NOW + 3600)
    assert action == "wait"
    assert state["manual"] is True


def test_captcha_wait_persists_without_stopping_login_page():
    state, action = decide(error="verification_required")
    assert action == "wait"
    state, action = decide(state, now=NOW + 86400, error="none")
    assert action == "wait"
    assert state["status"] == "manual_login_required"


def test_startup_waits_for_quick_login():
    state, action = decide(started=NOW - 10, error="session_expired")
    assert action == "wait"
    assert state["status"] == "login_starting"


def test_transient_offline_recovers_without_restart():
    state, action = decide()
    assert action == "wait"
    state, action = decide(state, now=NOW + 240, online=True)
    assert action == "wait"
    assert state["restarts"] == []


def test_restart_cooldown_and_daily_limit():
    state, action = decide({"first_offline": NOW - 300})
    assert action == "restart"
    state, action = decide(state, now=NOW + 600)
    assert action == "wait"
    state, action = decide(state, now=NOW + 1800)
    assert action == "restart"
    state, action = decide(state, now=NOW + 4000)
    assert action == "wait"
    state, action = decide(state, now=NOW + 5400)
    assert action == "restart"
    state, action = decide(state, now=NOW + 13000)
    assert action == "wait"
    assert state["manual"] is True


def test_missing_password_requires_manual_login():
    state, action = decide(error="session_expired", has_password=False)
    assert action == "wait"
    assert state["manual"] is True


@pytest.mark.parametrize("message,category", [
    ("你的用户身份已失效，请你重新登录。", "session_expired"),
    ("二维码已过期，请刷新", "session_expired"),
    ("需要验证码，暂不支持", "verification_required"),
    ("账号安全风险部分功能使用受限", "verification_required"),
    ("unknown error", "login_error"),
])
def test_login_error_classification(message, category):
    assert watchdog.classify_error(message) == category


@pytest.mark.parametrize("login_response", [None,
    {"code": 0, "data": {"needCaptcha": True}},
    {"code": 0, "data": {"needNewDevice": True}},
])
def test_password_challenge_or_timeout_is_persisted_and_never_repeated(tmp_path, monkeypatch, login_response):
    monkeypatch.setattr(watchdog, "STATE_DIR", tmp_path)
    monkeypatch.setattr(watchdog.time, "time", lambda: NOW)
    monkeypatch.setattr(watchdog.time, "monotonic", lambda: 1000)
    monkeypatch.setattr(watchdog, "password_credentials", lambda: ("123", "a" * 32))
    calls = []

    def systemctl(*args):
        assert args[0] == "show"  # Never stop/restart while waiting for verification.
        return "ActiveState=active\nActiveEnterTimestampMonotonic=1000000"

    class FakeAPI:
        def call(self, path, data=None):
            if path.endswith("PasswordLogin"):
                calls.append(path)
                assert watchdog.json.loads((tmp_path / "state.json").read_text())["password_attempted"]
                if login_response is None:
                    raise TimeoutError("secret data should not be printed")
                return login_response
            return {"code": 0, "data": {"isLogin": False, "loginError": "登录态失效"}}

    monkeypatch.setattr(watchdog, "systemctl", systemctl)
    monkeypatch.setattr(watchdog, "WebUI", FakeAPI)
    watchdog.tick()
    state = watchdog.tick()
    assert len(calls) == 1
    assert state["status"] == "manual_login_required"
    assert (tmp_path / "state.json").stat().st_mode & 0o777 == 0o600
