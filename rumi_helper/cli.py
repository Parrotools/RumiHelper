"""RumiHelper application entry point.

NapCatQQ connects through OneBot 11 reverse WebSocket at:
    ws://<HOST>:<PORT>/onebot/v11/ws
"""

import os

import nonebot
from fastapi.responses import JSONResponse
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

from rumi_helper.config import Settings


def _bridge_onebot_access_token() -> None:
    """Expose the project's legacy token name under the adapter's canonical key."""
    token = Settings().onebot_token
    if token:
        os.environ["ONEBOT_V11_ACCESS_TOKEN"] = token


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
async def readyz():
    """核心就绪检查；QQ/GitHub 短暂异常不会让实例失去就绪状态。"""
    from rumi_helper.services.runtime import get_runtime

    try:
        result = await get_runtime().health.check_readiness()
    except RuntimeError:
        return JSONResponse({"ok": False, "detail": "starting"}, status_code=503)
    return JSONResponse(result, status_code=200 if result["ok"] else 503)


@driver.server_app.get("/healthz")
async def healthz():
    """完整业务健康检查，包括 QQ 与 GitHub 等外部依赖。"""
    from rumi_helper.services.runtime import get_runtime

    try:
        result = await get_runtime().health.check(include_diagnostics=False)
    except RuntimeError:
        return JSONResponse({"ok": False, "detail": "starting"}, status_code=503)
    return JSONResponse(result, status_code=200 if result["ok"] else 503)


def main() -> None:
    """Console entry point for source and wheel installations."""
    nonebot.run()


if __name__ == "__main__":
    main()
