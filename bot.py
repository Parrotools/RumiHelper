"""qq-llm-bot 入口。

启动方式（项目根目录）：
    python bot.py

NapCatQQ 通过 OneBot 11 反向 WebSocket 连接到本进程：
    Bot 侧监听  ws://<HOST>:<PORT>/onebot/v11/ws
"""

import os
from pathlib import Path

import nonebot
from fastapi.responses import JSONResponse
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

nonebot.init()

driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

def _load_packaged_plugins() -> None:
    """Load plugins without requiring the process cwd to contain app/plugins."""
    package_root = Path(__file__).resolve().parent
    original_cwd = Path.cwd()
    try:
        os.chdir(package_root)
        nonebot.load_plugins(str(package_root / "app" / "plugins"))
    finally:
        os.chdir(original_cwd)


_load_packaged_plugins()


async def _startup() -> None:
    from app.services.runtime import init_runtime

    await init_runtime()


async def _shutdown() -> None:
    from app.services.runtime import close_runtime

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
    from app.services.runtime import get_runtime

    try:
        result = await get_runtime().health.check_readiness()
    except RuntimeError:
        return JSONResponse({"ok": False, "detail": "starting"}, status_code=503)
    return JSONResponse(result, status_code=200 if result["ok"] else 503)


@driver.server_app.get("/healthz")
async def healthz():
    """完整业务健康检查，包括 QQ 与 GitHub 等外部依赖。"""
    from app.services.runtime import get_runtime

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
