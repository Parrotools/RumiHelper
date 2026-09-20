"""RumiHelper application entry point.

NapCatQQ connects through OneBot 11 reverse WebSocket at:
    ws://<HOST>:<PORT>/onebot/v11/ws
"""

import os
import secrets

import nonebot
from fastapi import Request
from fastapi.responses import JSONResponse
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

from rumi_helper.config import Settings

_ONEBOT_ACCESS_TOKEN = Settings().onebot_token


def _bridge_onebot_access_token() -> None:
    """Expose the project's legacy token name under the adapter's canonical key."""
    if _ONEBOT_ACCESS_TOKEN:
        os.environ["ONEBOT_V11_ACCESS_TOKEN"] = _ONEBOT_ACCESS_TOKEN


def _probe_authorized(request: Request) -> bool:
    if not _ONEBOT_ACCESS_TOKEN:
        return True
    supplied = request.headers.get("authorization", "")
    return secrets.compare_digest(
        supplied,
        f"Bearer {_ONEBOT_ACCESS_TOKEN}",
    )


def _probe_unauthorized() -> JSONResponse:
    return JSONResponse(
        {"ok": False},
        status_code=401,
        headers={"WWW-Authenticate": "Bearer"},
    )


_bridge_onebot_access_token()
nonebot.init()

driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)


def _load_packaged_plugins() -> None:
    """Load packaged plugins by import path, independent of process cwd."""
    plugin_names = (
        "event_dedup",
        "admin",
        "agent",
        "ai_chat",
        "broadcast",
        "github",
        "group_feedback",
        "memory",
        "report",
        "scheduler",
        "web_summary",
    )
    for name in plugin_names:
        nonebot.load_plugin(f"rumi_helper.plugins.{name}")


_load_packaged_plugins()


async def _startup() -> None:
    from rumi_helper.services.runtime import init_runtime

    await init_runtime()


async def _shutdown() -> None:
    from rumi_helper.services.runtime import close_runtime

    await close_runtime()


driver.on_startup(_startup)
driver.on_shutdown(_shutdown)


@driver.server_app.get("/livez")
async def livez():
    """进程级存活检查；不依赖任何外部服务。"""
    return JSONResponse({"ok": True}, status_code=200)


@driver.server_app.get("/readyz")
async def readyz(request: Request):
    """核心就绪检查；配置 OneBot token 时要求 Bearer 鉴权。"""
    if not _probe_authorized(request):
        return _probe_unauthorized()

    from rumi_helper.services.runtime import get_runtime

    try:
        result = await get_runtime().health.check_readiness()
    except RuntimeError:
        return JSONResponse({"ok": False}, status_code=503)
    ok = bool(result["ok"])
    return JSONResponse({"ok": ok}, status_code=200 if ok else 503)


@driver.server_app.get("/healthz")
async def healthz(request: Request):
    """完整业务健康检查；配置 OneBot token 时要求 Bearer 鉴权。"""
    if not _probe_authorized(request):
        return _probe_unauthorized()

    from rumi_helper.services.runtime import get_runtime

    try:
        result = await get_runtime().health.check(include_diagnostics=False)
    except RuntimeError:
        return JSONResponse({"ok": False}, status_code=503)
    ok = bool(result["ok"])
    return JSONResponse({"ok": ok}, status_code=200 if ok else 503)


def main() -> None:
    """Console entry point for source and wheel installations."""
    nonebot.run()


if __name__ == "__main__":
    main()
