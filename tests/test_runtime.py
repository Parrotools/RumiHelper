import asyncio
from typing import ClassVar

import pytest

import rumi_helper.services.runtime as runtime_module
from rumi_helper.config import Settings
from rumi_helper.database.db import Database


class TrackingDatabase(Database):
    instances: ClassVar[list["TrackingDatabase"]] = []

    def __init__(self, path: str):
        super().__init__(path)
        self.closed = False
        self.__class__.instances.append(self)

    async def close(self) -> None:
        self.closed = True
        await super().close()


class ClosableDependency:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_init_runtime_failure_unwinds_resources_and_does_not_publish(
    tmp_path,
    monkeypatch,
):
    settings = Settings(
        database_path=str(tmp_path / "runtime.db"),
        github_check_cron="",
        github_digest_cron="",
        daily_report_cron="",
        enable_playwright=False,
    )
    fake_llm = ClosableDependency()
    fake_github_client = ClosableDependency()
    fake_fetcher = ClosableDependency()
    TrackingDatabase.instances.clear()

    monkeypatch.setattr(runtime_module, "_runtime", None)
    monkeypatch.setattr(runtime_module, "_runtime_lock", asyncio.Lock())
    monkeypatch.setattr(runtime_module, "get_settings", lambda: settings)
    monkeypatch.setattr(runtime_module, "Database", TrackingDatabase)
    monkeypatch.setattr(
        runtime_module,
        "OpenAICompatibleProvider",
        lambda **_kwargs: fake_llm,
    )
    monkeypatch.setattr(
        runtime_module,
        "GitHubClient",
        lambda **_kwargs: fake_github_client,
    )
    monkeypatch.setattr(
        runtime_module,
        "WebFetcher",
        lambda **_kwargs: fake_fetcher,
    )

    async def fail_start(_self) -> None:
        raise RuntimeError("scheduler startup failed")

    monkeypatch.setattr(runtime_module.SchedulerService, "start", fail_start)

    with pytest.raises(RuntimeError, match="scheduler startup failed"):
        await runtime_module.init_runtime()

    assert runtime_module._runtime is None
    assert len(TrackingDatabase.instances) == 1
    assert TrackingDatabase.instances[0].closed is True
    assert fake_llm.closed is True
    assert fake_github_client.closed is True
    assert fake_fetcher.closed is True

    await asyncio.sleep(0)
    assert all(
        task.get_name() != "outbound-message-worker"
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
    )


@pytest.mark.asyncio
async def test_concurrent_init_runtime_builds_one_instance(tmp_path, monkeypatch):
    settings = Settings(
        database_path=str(tmp_path / "concurrent-runtime.db"),
        github_check_cron="",
        github_digest_cron="",
        daily_report_cron="",
        enable_playwright=False,
    )
    fake_llm = ClosableDependency()
    fake_github_client = ClosableDependency()
    fake_fetcher = ClosableDependency()
    TrackingDatabase.instances.clear()

    monkeypatch.setattr(runtime_module, "_runtime", None)
    monkeypatch.setattr(runtime_module, "_runtime_lock", asyncio.Lock())
    monkeypatch.setattr(runtime_module, "get_settings", lambda: settings)
    monkeypatch.setattr(runtime_module, "Database", TrackingDatabase)
    monkeypatch.setattr(
        runtime_module,
        "OpenAICompatibleProvider",
        lambda **_kwargs: fake_llm,
    )
    monkeypatch.setattr(
        runtime_module,
        "GitHubClient",
        lambda **_kwargs: fake_github_client,
    )
    monkeypatch.setattr(
        runtime_module,
        "WebFetcher",
        lambda **_kwargs: fake_fetcher,
    )

    original_start = runtime_module.SchedulerService.start
    entered = asyncio.Event()
    release = asyncio.Event()

    async def gated_start(self) -> None:
        entered.set()
        await release.wait()
        await original_start(self)

    monkeypatch.setattr(runtime_module.SchedulerService, "start", gated_start)

    first = asyncio.create_task(runtime_module.init_runtime())
    await entered.wait()
    second = asyncio.create_task(runtime_module.init_runtime())
    await asyncio.sleep(0)

    assert second.done() is False
    assert len(TrackingDatabase.instances) == 1

    release.set()
    first_runtime, second_runtime = await asyncio.gather(first, second)

    assert first_runtime is second_runtime
    assert runtime_module._runtime is first_runtime
    assert len(TrackingDatabase.instances) == 1

    await runtime_module.close_runtime()
    assert runtime_module._runtime is None
    assert TrackingDatabase.instances[0].closed is True
    assert fake_llm.closed is True
    assert fake_github_client.closed is True
    assert fake_fetcher.closed is True
