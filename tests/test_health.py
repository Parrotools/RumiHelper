import asyncio
from types import SimpleNamespace

import pytest

from rumi_helper.database.db import Database
from rumi_helper.services.health import HealthService, format_status


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "health.db"))
    await database.connect()
    yield database
    await database.close()


class HealthyDependency:
    async def check_health(self):
        return None


class FailedDependency:
    async def check_health(self):
        raise RuntimeError("unavailable")


class CountingDependency:
    def __init__(self):
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def check_health(self):
        self.calls += 1
        self.entered.set()
        await self.release.wait()


async def test_health_reports_dependencies_counts_and_recent_error(db):
    await db.insert_memory("user-1", "fact", "重要事项", 0.9)
    await db.insert_github_repository("user-1", "OpenAI", "openai-python", "https://github.com/OpenAI/openai-python")
    await db.insert_send_log("user", "123", "内容", False, None, "send failed")
    health = HealthService(
        db,
        HealthyDependency(),
        HealthyDependency(),
        SimpleNamespace(is_running=True, job_count=2),
        started_at=0,
    )

    result = await health.check()

    assert result["database"]["ok"] is True
    assert result["llm"]["ok"] is True
    assert result["github"]["ok"] is True
    assert result["scheduler"] == {"running": True, "jobs": 2}
    assert result["counts"]["github_repositories"] == 1
    assert result["counts"]["memories"] == 1
    assert result["recent_errors"][0]["error"] == "send failed"


async def test_health_isolates_dependency_failure(db):
    health = HealthService(
        db,
        FailedDependency(),
        HealthyDependency(),
        SimpleNamespace(is_running=False, job_count=0),
        started_at=0,
    )

    result = await health.check()

    assert result["database"]["ok"] is True
    assert result["llm"] == {"ok": False, "detail": "RuntimeError"}


@pytest.mark.parametrize("response", [
    {"online": False, "good": False}, {}, None,
    {"online": "true", "good": True}, {"online": True},
])
async def test_health_can_probe_qq_status(db, monkeypatch, response):
    class FakeBot:
        async def call_api(self, action):
            assert action == "get_status"
            return response

    monkeypatch.setattr("nonebot.get_bots", lambda: {"bot": FakeBot()})
    health = HealthService(
        db,
        HealthyDependency(),
        HealthyDependency(),
        SimpleNamespace(is_running=True, job_count=0),
        started_at=0,
        check_qq=True,
    )

    result = await health.check()

    assert result["ok"] is False
    assert result["qq"] == {"ok": False, "detail": "RuntimeError"}


async def test_readiness_checks_only_core_dependencies(db):
    health = HealthService(
        db,
        HealthyDependency(),
        FailedDependency(),
        SimpleNamespace(is_running=True, job_count=3),
        started_at=0,
        check_qq=True,
    )

    result = await health.check_readiness()

    assert result["ok"] is True
    assert result["database"]["ok"] is True
    assert result["llm"]["ok"] is True
    assert result["scheduler"] == {"running": True, "jobs": 3}
    assert "github" not in result
    assert "qq" not in result


async def test_readiness_requires_running_scheduler(db):
    health = HealthService(
        db,
        HealthyDependency(),
        HealthyDependency(),
        SimpleNamespace(is_running=False, job_count=0),
        started_at=0,
    )

    result = await health.check_readiness()

    assert result["ok"] is False
    assert result["scheduler"]["running"] is False


async def test_full_health_fails_when_scheduler_is_stopped(db):
    health = HealthService(
        db,
        HealthyDependency(),
        HealthyDependency(),
        SimpleNamespace(is_running=False, job_count=0),
        started_at=0,
    )

    result = await health.check()

    assert result["database"]["ok"] is True
    assert result["llm"]["ok"] is True
    assert result["github"]["ok"] is True
    assert result["scheduler"]["running"] is False
    assert result["ok"] is False


def test_public_status_hides_operational_diagnostics():
    status = {
        "uptime_seconds": 42,
        "database": {"ok": True},
        "llm": {"ok": True},
        "scheduler": {"running": True, "jobs": 7},
        "counts": {"github_repositories": 9, "memories": 11},
        "recent_errors": [{"error": "sensitive backend detail"}],
    }

    rendered = format_status(status, detailed=False)

    assert "服务：正常" in rendered
    assert "运行时间：42 秒" in rendered
    assert "GitHub 监控" not in rendered
    assert "Memory" not in rendered
    assert "最近错误" not in rendered
    assert "sensitive backend detail" not in rendered


def test_admin_status_keeps_operational_diagnostics():
    status = {
        "uptime_seconds": 42,
        "database": {"ok": True},
        "llm": {"ok": True},
        "scheduler": {"running": True, "jobs": 7},
        "counts": {"github_repositories": 9, "memories": 11},
        "recent_errors": [{"error": "send failed"}],
    }

    rendered = format_status(status, detailed=True)

    assert "Scheduler：运行中（7 个任务）" in rendered
    assert "主动队列：运行中" in rendered
    assert "GitHub 监控：9" in rendered
    assert "Memory：11" in rendered
    assert "最近错误：send failed" in rendered


async def test_readiness_cache_avoids_repeated_external_probe(db):
    llm = CountingDependency()
    health = HealthService(
        db,
        llm,
        HealthyDependency(),
        SimpleNamespace(is_running=True, job_count=1),
        started_at=0,
        cache_ttl_seconds=60,
    )

    first = await health.check_readiness()
    second = await health.check_readiness()

    assert first == second
    assert llm.calls == 1


async def test_concurrent_readiness_requests_share_one_probe(db):
    llm = CountingDependency()
    llm.release.clear()
    health = HealthService(
        db,
        llm,
        HealthyDependency(),
        SimpleNamespace(is_running=True, job_count=1),
        started_at=0,
        cache_ttl_seconds=60,
    )

    first = asyncio.create_task(health.check_readiness())
    second = asyncio.create_task(health.check_readiness())
    await llm.entered.wait()
    await asyncio.sleep(0)

    assert llm.calls == 1
    llm.release.set()

    assert await first == await second
    assert llm.calls == 1


async def test_detailed_health_bypasses_public_probe_cache(db):
    llm = CountingDependency()
    github = CountingDependency()
    health = HealthService(
        db,
        llm,
        github,
        SimpleNamespace(is_running=True, job_count=1),
        started_at=0,
        cache_ttl_seconds=60,
    )

    public = await health.check(include_diagnostics=False)
    detailed = await health.check(include_diagnostics=True)

    assert public["ok"] is True
    assert detailed["ok"] is True
    assert "counts" not in public
    assert "counts" in detailed
    assert llm.calls == 2
    assert github.calls == 2


async def test_health_can_omit_operational_diagnostics(db):
    await db.insert_send_log("user", "123", "内容", False, None, "secret-ish error")
    health = HealthService(
        db,
        HealthyDependency(),
        HealthyDependency(),
        SimpleNamespace(is_running=True, job_count=2),
        started_at=0,
    )

    result = await health.check(include_diagnostics=False)

    assert result["ok"] is True
    assert "counts" not in result
    assert "recent_errors" not in result


async def test_health_fails_when_outbound_worker_is_stopped(db):
    dispatcher = SimpleNamespace(worker_running=False)
    health = HealthService(
        db,
        HealthyDependency(),
        HealthyDependency(),
        SimpleNamespace(is_running=True, job_count=1),
        started_at=0,
        dispatcher=dispatcher,
        cache_ttl_seconds=0,
    )

    readiness = await health.check_readiness()
    full = await health.check()

    assert readiness["outbound_worker"] == {"running": False}
    assert readiness["ok"] is False
    assert full["outbound_worker"] == {"running": False}
    assert full["ok"] is False


def test_public_status_reports_abnormal_when_outbound_worker_is_stopped():
    status = {
        "uptime_seconds": 42,
        "database": {"ok": True},
        "llm": {"ok": True},
        "scheduler": {"running": True, "jobs": 1},
        "outbound_worker": {"running": False},
    }

    rendered = format_status(status, detailed=False)

    assert "服务：异常" in rendered
