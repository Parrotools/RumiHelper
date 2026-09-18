"""运行依赖健康检查与安全状态汇总。"""

import asyncio
import copy
import time
from collections.abc import Awaitable, Callable
from typing import Any

from rumi_helper.database.db import Database


def format_status(status: dict, *, detailed: bool) -> str:
    """Render public status conservatively; operational diagnostics are admin-only."""
    core_ok = bool(
        status.get("database", {}).get("ok")
        and status.get("llm", {}).get("ok")
        and status.get("scheduler", {}).get("running")
    )
    lines = [
        "qq-llm-bot 运行状态：",
        f"服务：{'正常' if core_ok else '异常'}",
        f"运行时间：{status.get('uptime_seconds', 0)} 秒",
    ]
    if not detailed:
        return "\n".join(lines)

    counts = status.get("counts", {})
    errors = status.get("recent_errors", [])
    lines.extend(
        [
            f"LLM：{'正常' if status.get('llm', {}).get('ok') else '异常'}",
            f"数据库：{'正常' if status.get('database', {}).get('ok') else '异常'}",
            (
                f"Scheduler：{'运行中' if status.get('scheduler', {}).get('running') else '未运行'}"
                f"（{status.get('scheduler', {}).get('jobs', 0)} 个任务）"
            ),
            f"GitHub 监控：{counts.get('github_repositories', 0)}",
            f"Memory：{counts.get('memories', 0)}",
            f"最近错误：{errors[0]['error'] if errors else '无'}",
        ]
    )
    return "\n".join(lines)


class HealthService:
    def __init__(
        self,
        db: Database,
        llm: Any,
        github: Any,
        scheduler: Any,
        started_at: float,
        timeout: float = 3.0,
        check_qq: bool = False,
        cache_ttl_seconds: float = 2.0,
    ):
        self._db = db
        self._llm = llm
        self._github = github
        self._scheduler = scheduler
        self._started_at = started_at
        self._timeout = max(0.1, timeout)
        self._check_qq = check_qq
        self._cache_ttl_seconds = max(0.0, float(cache_ttl_seconds))
        self._probe_cache: dict[str, tuple[float, dict]] = {}
        self._probe_inflight: dict[str, asyncio.Task[dict]] = {}

    async def check_readiness(self) -> dict:
        """核心就绪检查：短缓存并合并并发 probe，避免健康检查放大外部调用。"""
        return await self._cached_probe("readiness", self._check_readiness_uncached)

    async def _check_readiness_uncached(self) -> dict:
        database, llm = await asyncio.gather(
            self._probe(self._database_probe),
            self._probe(self._llm.check_health),
        )
        scheduler = {"running": self._scheduler.is_running, "jobs": self._scheduler.job_count}
        return {
            "ok": database["ok"] and llm["ok"] and scheduler["running"],
            "uptime_seconds": int(time.monotonic() - self._started_at),
            "database": database,
            "llm": llm,
            "scheduler": scheduler,
        }

    async def check(self, *, include_diagnostics: bool = True) -> dict:
        if not include_diagnostics:
            return await self._cached_probe(
                "full",
                lambda: self._check_uncached(include_diagnostics=False),
            )
        return await self._check_uncached(include_diagnostics=True)

    async def _check_uncached(self, *, include_diagnostics: bool) -> dict:
        probes = [
            self._probe(self._database_probe),
            self._probe(self._llm.check_health),
            self._probe(self._github.check_health),
        ]
        if self._check_qq:
            probes.append(self._probe(self._qq_probe))
        database, llm, github, *qq_results = await asyncio.gather(*probes)
        scheduler = {"running": self._scheduler.is_running, "jobs": self._scheduler.job_count}
        result = {
            "ok": all(item["ok"] for item in (database, llm, github, *qq_results)) and scheduler["running"],
            "uptime_seconds": int(time.monotonic() - self._started_at),
            "database": database,
            "llm": llm,
            "github": github,
            "scheduler": scheduler,
        }
        if include_diagnostics:
            result["counts"] = await self._counts() if database["ok"] else {}
            result["recent_errors"] = await self._db.fetch_recent_errors() if database["ok"] else []
        if qq_results:
            result["qq"] = qq_results[0]
        return result

    async def _cached_probe(
        self,
        key: str,
        operation: Callable[[], Awaitable[dict]],
    ) -> dict:
        now = time.monotonic()
        cached = self._probe_cache.get(key)
        if cached is not None and now - cached[0] <= self._cache_ttl_seconds:
            return copy.deepcopy(cached[1])

        task = self._probe_inflight.get(key)
        if task is None:
            task = asyncio.create_task(operation())
            self._probe_inflight[key] = task
            task.add_done_callback(
                lambda done, cache_key=key: self._forget_probe(cache_key, done)
            )

        result = await asyncio.shield(task)
        if self._cache_ttl_seconds > 0:
            self._probe_cache[key] = (time.monotonic(), copy.deepcopy(result))
        return copy.deepcopy(result)

    def _forget_probe(self, key: str, task: asyncio.Task[dict]) -> None:
        if self._probe_inflight.get(key) is task:
            self._probe_inflight.pop(key, None)

    async def _qq_probe(self) -> None:
        """检查 OneBot 是否连着一个在线的 QQ 账号，而不只检查进程是否存在。"""
        from nonebot import get_bots

        bots = get_bots()
        if not bots:
            raise RuntimeError("OneBot 未连接")
        bot = next(iter(bots.values()))
        status = await bot.call_api("get_status")
        if not isinstance(status, dict) or status.get("online") is not True:
            raise RuntimeError("QQ 在线状态未确认")
        if status.get("good") is not True:
            raise RuntimeError("OneBot 状态未确认")

    async def _database_probe(self) -> None:
        await self._db.fetchone("SELECT 1 AS ok", ())

    async def _probe(self, operation) -> dict:
        try:
            await asyncio.wait_for(operation(), timeout=self._timeout)
        except TimeoutError:
            return {"ok": False, "detail": "Timeout"}
        except Exception as e:  # noqa: BLE001 — 健康检查必须隔离任意依赖失败
            return {"ok": False, "detail": type(e).__name__}
        return {"ok": True}

    async def _counts(self) -> dict[str, int | dict[str, int]]:
        github = await self._db.fetchone("SELECT COUNT(*) AS count FROM github_repositories", ())
        memories = await self._db.fetchone("SELECT COUNT(*) AS count FROM memories", ())
        return {
            "github_repositories": int(github["count"]),
            "memories": int(memories["count"]),
            "outbound_messages": await self._db.outbound_message_counts(),
        }
