"""Bounded recovery using NapCat's WebUI; never print credentials or login errors."""

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess  # nosec B404
import tempfile
import time
import urllib.request
from pathlib import Path

STATE_DIR = Path("/var/lib/napcat-watchdog")
WEBUI_CONFIG = Path("/root/Napcat/opt/QQ/resources/app/app_launcher/napcat/config/webui.json")


def classify_error(message: str) -> str:
    if any(word in message for word in ("验证码", "新设备", "安全风险", "受限", "冻结", "密码错误", "密码不正确")):
        return "verification_required"
    if "二维码" in message and any(word in message for word in ("过期", "失效")):
        # QR expiry can overwrite the quick-login error while we wait for startup.
        # It is not an account restriction and does not prohibit password login.
        return "session_expired"
    if any(word in message for word in ("失效", "重新登录", "历史登录记录")):
        return "session_expired"
    return "login_error" if message else "none"


def decide(state: dict, *, now: float, running: bool, started: float, online: bool | None,
           error: str, has_password: bool) -> tuple[dict, str]:
    """Preserve incident/retry state across process restarts and observation gaps."""
    state = dict(state)
    state["observed_at"] = now
    state["restarts"] = [t for t in state.get("restarts", []) if now - t < 86400]
    if not running:
        state["status"] = "service_stopped"
        return state, "wait"
    if online is True:
        state.update(status="online", manual=False, first_offline=None, password_attempted=False)
        return state, "wait"
    if state.get("manual"):
        state["status"] = "manual_login_required"
        return state, "wait"
    if online is None:
        # A probe timeout does not authorize restarting a live process.
        state["status"] = "probe_unavailable"
        return state, "wait"
    if error in ("verification_required", "login_error"):
        state.update(status="manual_login_required", manual=True)
        return state, "wait"
    if now - started < 120:
        state["status"] = "login_starting"
        return state, "wait"
    if error == "session_expired":
        if (has_password and not state.get("password_attempted")
                and now - state.get("last_password_at", 0) >= 86400):
            # Persist before requesting login; a timeout must never cause a repeat.
            state.update(status="password_login_pending", password_attempted=True, last_password_at=now)
            return state, "password"
        state.update(status="manual_login_required", manual=True)
        return state, "wait"
    if state.get("first_offline") is None:
        state["first_offline"] = now
    state["status"] = "offline_waiting"
    if now - state["first_offline"] < 300:
        return state, "wait"
    restarts = state["restarts"]
    if len(restarts) >= 3:
        state.update(status="recovery_limit_reached", manual=True)
        return state, "wait"
    if restarts and now - restarts[-1] < 1800 * (2 ** (len(restarts) - 1)):
        return state, "wait"
    state.update(status="restarting_once", first_offline=now, restarts=[*restarts, now])
    return state, "restart"


class WebUI:
    def __init__(self, config_path: Path = WEBUI_CONFIG):
        config = json.loads(config_path.read_text())
        token = config.get("token")
        if not isinstance(token, str) or not token:
            raise ValueError("missing WebUI credential")
        self.base = "http://127.0.0.1:" + str(int(config.get("port", 6099))) + "/api"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.credential = ""
        response = self.call("/auth/login", {"hash": hashlib.sha256((token + ".napcat").encode()).hexdigest()})
        self.credential = response["data"]["Credential"]

    def call(self, path: str, data: dict | None = None) -> dict:
        request = urllib.request.Request(
            self.base + path, json.dumps(data or {}).encode(),
            {"Content-Type": "application/json", "Authorization": "Bearer " + self.credential},
        )
        with self.opener.open(request, timeout=15) as response:
            result = json.load(response)
        if not isinstance(result, dict):
            raise TypeError("invalid WebUI response")
        return result


def save(path: Path, data: dict) -> None:
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".state-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def systemctl(*args: str) -> str:
    # Fixed argv, absolute executable, no shell; callers pass internal constant arguments only.
    return subprocess.run(  # nosec B603
        ["/usr/bin/systemctl", *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=45,
    ).stdout.strip()


def password_credentials() -> tuple[str, str]:
    account = os.environ.get("NAPCAT_QUICK_ACCOUNT", "")
    digest = os.environ.get("NAPCAT_QUICK_PASSWORD_MD5", "")
    password = os.environ.get("NAPCAT_QUICK_PASSWORD", "")
    if not digest and password:
        digest = hashlib.md5(password.encode(), usedforsecurity=False).hexdigest()
    if not account.isdigit() or not re.fullmatch(r"[0-9a-fA-F]{32}", digest):
        return "", ""
    return account, digest


def tick(recover: bool = False) -> dict:
    state_path = STATE_DIR / "state.json"
    # Corrupt state fails closed, instead of silently resetting retry budgets.
    state = json.loads(state_path.read_text()) if state_path.exists() else {
        "manual": (STATE_DIR / "manual-auth-required").exists(),
    }
    if recover:
        state.update(manual=False, first_offline=None)
        save(state_path, state)
        active = systemctl("show", "napcat.service", "-p", "ActiveState", "--value")
        if active in ("inactive", "failed"):
            systemctl("start", "napcat.service")
    props = dict(line.split("=", 1) for line in systemctl(
        "show", "napcat.service", "-p", "ActiveState,ActiveEnterTimestampMonotonic"
    ).splitlines() if "=" in line)
    running = props.get("ActiveState") == "active"
    now = time.time()
    started = now - (time.monotonic() - int(props.get("ActiveEnterTimestampMonotonic", "0")) / 1_000_000)
    online, error, api = None, "none", None
    if running:
        try:
            api = WebUI()
            response = api.call("/QQLogin/CheckLoginStatus")
            if response.get("code") == 0:
                data = response.get("data", {})
                value = data.get("isLogin")
                online = value if isinstance(value, bool) else None
                error = classify_error(str(data.get("loginError", "")))
        except Exception:  # noqa: BLE001 — unknown probe failures must never trigger a restart
            online = None
    account, digest = password_credentials()
    previous = state.get("status")
    state, action = decide(state, now=now, running=running, started=started,
                           online=online, error=error, has_password=bool(account and digest))
    save(state_path, state)
    if state.get("status") != previous:
        print("napcat-watchdog status=" + state["status"], flush=True)
    if action == "restart":
        systemctl("restart", "napcat.service")
    elif action == "password":
        try:
            response = api.call("/QQLogin/PasswordLogin", {"uin": account, "passwordMd5": digest})
            # Newer NapCat returns code=0 with a challenge, not a completed login.
            data = response.get("data") or {}
            success = (response.get("code") == 0 and isinstance(data, dict)
                       and not data.get("needCaptcha") and not data.get("needNewDevice"))
        except Exception:  # noqa: BLE001 — ambiguous login results must not be retried
            success = False
        state.update(manual=not success, status="login_pending" if success else "manual_login_required")
        save(state_path, state)
        print("napcat-watchdog status=" + state["status"], flush=True)
    # Keep NapCat and its verification page alive when manual login is required.
    return state


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--recover", action="store_true", help="explicit recovery; preserves daily budgets")
    args = parser.parse_args()
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (STATE_DIR / "watchdog.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        try:
            tick(args.recover)
        except Exception as exc:  # noqa: BLE001 — avoid logging credentials embedded in errors
            print("napcat-watchdog inspection_failed=" + type(exc).__name__, flush=True)
            raise SystemExit(1) from None


if __name__ == "__main__":
    main()
