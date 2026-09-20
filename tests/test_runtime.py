import asyncio
from typing import ClassVar

import pytest

import rumi_helper.services.runtime as runtime_module
from rumi_helper.config import Settings
from rumi_helper.config_validation import RuntimeConfigurationError
from rumi_helper.database.db import Database
from rumi_helper.security.config import SecurityConfigurationError


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
        task.get_name() not in {
            "outbound-message-worker",
            "confirmation-housekeeping",
        }
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
    housekeeping_task = first_runtime.housekeeping_task
    assert housekeeping_task is not None
    assert housekeeping_task.done() is False

    await runtime_module.close_runtime()
    assert runtime_module._runtime is None
    assert housekeeping_task.done() is True
    assert TrackingDatabase.instances[0].closed is True
    assert fake_llm.closed is True
    assert fake_github_client.closed is True
    assert fake_fetcher.closed is True


@pytest.mark.asyncio
async def test_init_runtime_marks_stale_executing_actions_failed(tmp_path, monkeypatch):
    database_path = str(tmp_path / "recover-runtime.db")
    seed = Database(database_path)
    await seed.connect()
    action_id = await seed.create_pending_agent_action(
        "user-1",
        "private:user-1",
        "memory_save",
        "{}",
        "expired",
        "2001-01-01T00:00:00+00:00",
    )
    claimed = await seed.claim_pending_agent_action(
        "user-1",
        "private:user-1",
        "2000-01-01T00:00:00+00:00",
    )
    assert claimed is not None and claimed["id"] == action_id
    fresh_action_id = await seed.create_pending_agent_action(
        "user-2",
        "private:user-2",
        "memory_save",
        "{}",
        "fresh",
        "9999-01-01T00:00:00+00:00",
    )
    fresh_claimed = await seed.claim_pending_agent_action(
        "user-2",
        "private:user-2",
        "2000-01-01T00:00:00+00:00",
    )
    assert fresh_claimed is not None and fresh_claimed["id"] == fresh_action_id

    broadcast_id = await seed.create_pending_broadcast(
        "admin-1",
        [{"type": "user", "id": "123"}],
        "expired",
        ttl_seconds=300,
    )
    assert await seed.claim_pending(broadcast_id) is True
    await seed.execute(
        "UPDATE pending_broadcasts SET expires_at = ? WHERE id = ?",
        ("2001-01-01 00:00:00", broadcast_id),
    )
    fresh_broadcast_id = await seed.create_pending_broadcast(
        "admin-2",
        [{"type": "user", "id": "456"}],
        "fresh",
        ttl_seconds=300,
    )
    assert await seed.claim_pending(fresh_broadcast_id) is True
    await seed.execute(
        "UPDATE pending_broadcasts SET expires_at = ? WHERE id = ?",
        ("9999-01-01 00:00:00", fresh_broadcast_id),
    )
    await seed.close()

    settings = Settings(
        database_path=database_path,
        github_check_cron="",
        github_digest_cron="",
        daily_report_cron="",
        enable_playwright=False,
    )
    fake_llm = ClosableDependency()
    fake_github_client = ClosableDependency()
    fake_fetcher = ClosableDependency()

    monkeypatch.setattr(runtime_module, "_runtime", None)
    monkeypatch.setattr(runtime_module, "_runtime_lock", asyncio.Lock())
    monkeypatch.setattr(runtime_module, "get_settings", lambda: settings)
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

    runtime = await runtime_module.init_runtime()
    try:
        assert await runtime.db.fetchone(
            "SELECT status FROM pending_agent_actions WHERE id = ?",
            (action_id,),
        ) == {"status": "failed"}
        assert await runtime.db.fetchone(
            "SELECT status FROM pending_broadcasts WHERE id = ?",
            (broadcast_id,),
        ) == {"status": "failed"}
        assert await runtime.db.fetchone(
            "SELECT status FROM pending_agent_actions WHERE id = ?",
            (fresh_action_id,),
        ) == {"status": "executing"}
        assert await runtime.db.fetchone(
            "SELECT status FROM pending_broadcasts WHERE id = ?",
            (fresh_broadcast_id,),
        ) == {"status": "executing"}
    finally:
        await runtime_module.close_runtime()


@pytest.mark.asyncio
async def test_confirmation_housekeeping_expires_orphaned_executions(tmp_path):
    db = Database(str(tmp_path / "housekeeping.db"))
    await db.connect()
    action_id = await db.create_pending_agent_action(
        "user-1",
        "private:user-1",
        "memory_save",
        '{"content":"secret"}',
        "secret summary",
        "9999-01-01T00:00:00+00:00",
    )
    assert await db.claim_pending_agent_action(
        "user-1",
        "private:user-1",
        "2000-01-01T00:00:00+00:00",
    )
    broadcast_id = await db.create_pending_broadcast(
        "admin-1",
        [{"type": "user", "id": "123"}],
        "secret broadcast",
        ttl_seconds=300,
    )
    assert await db.claim_pending(broadcast_id)

    housekeeping = asyncio.create_task(
        runtime_module._confirmation_housekeeping_loop(
            db,
            interval_seconds=0.01,
        )
    )
    try:
        await db.execute(
            "UPDATE pending_agent_actions SET expires_at = ? WHERE id = ?",
            ("2001-01-01T00:00:00+00:00", action_id),
        )
        await db.execute(
            "UPDATE pending_broadcasts SET expires_at = ? WHERE id = ?",
            ("2001-01-01 00:00:00", broadcast_id),
        )

        for _ in range(50):
            action = await db.fetchone(
                "SELECT status, arguments, summary "
                "FROM pending_agent_actions WHERE id = ?",
                (action_id,),
            )
            broadcast = await db.fetchone(
                "SELECT status, targets_json, content "
                "FROM pending_broadcasts WHERE id = ?",
                (broadcast_id,),
            )
            if action["status"] == "failed" and broadcast["status"] == "failed":
                break
            await asyncio.sleep(0.01)

        assert action == {
            "status": "failed",
            "arguments": "{}",
            "summary": "",
        }
        assert broadcast == {
            "status": "failed",
            "targets_json": "[]",
            "content": "",
        }
    finally:
        housekeeping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await housekeeping
        await db.close()


@pytest.mark.asyncio
async def test_init_runtime_rejects_unsafe_config_before_opening_database(
    tmp_path,
    monkeypatch,
):
    settings = Settings(
        _env_file=None,
        database_path=str(tmp_path / "must-not-open.db"),
        host="0.0.0.0",
        onebot_access_token="",
        github_check_cron="",
        github_digest_cron="",
        daily_report_cron="",
    )
    TrackingDatabase.instances.clear()

    monkeypatch.setattr(runtime_module, "_runtime", None)
    monkeypatch.setattr(runtime_module, "_runtime_lock", asyncio.Lock())
    monkeypatch.setattr(runtime_module, "get_settings", lambda: settings)
    monkeypatch.setattr(runtime_module, "Database", TrackingDatabase)

    with pytest.raises(SecurityConfigurationError, match="ONEBOT_ACCESS_TOKEN"):
        await runtime_module.init_runtime()

    assert runtime_module._runtime is None
    assert TrackingDatabase.instances == []


@pytest.mark.asyncio
async def test_init_runtime_rejects_invalid_limits_before_creating_resources(
    tmp_path,
    monkeypatch,
):
    settings = Settings(
        _env_file=None,
        database_path=str(tmp_path / "must-not-exist.db"),
        send_rate_limit_per_second=0,
        github_check_cron="",
        github_digest_cron="",
        daily_report_cron="",
    )
    TrackingDatabase.instances.clear()

    monkeypatch.setattr(runtime_module, "_runtime", None)
    monkeypatch.setattr(runtime_module, "_runtime_lock", asyncio.Lock())
    monkeypatch.setattr(runtime_module, "get_settings", lambda: settings)
    monkeypatch.setattr(runtime_module, "Database", TrackingDatabase)

    with pytest.raises(
        RuntimeConfigurationError,
        match="send_rate_limit_per_second",
    ):
        await runtime_module.init_runtime()

    assert runtime_module._runtime is None
    assert TrackingDatabase.instances == []


@pytest.mark.asyncio
async def test_init_runtime_rejects_blank_llm_model_before_creating_resources(
    tmp_path,
    monkeypatch,
):
    settings = Settings(
        _env_file=None,
        database_path=str(tmp_path / "must-not-exist-model.db"),
        llm_model="   ",
        github_check_cron="",
        github_digest_cron="",
        daily_report_cron="",
    )
    TrackingDatabase.instances.clear()

    monkeypatch.setattr(runtime_module, "_runtime", None)
    monkeypatch.setattr(runtime_module, "_runtime_lock", asyncio.Lock())
    monkeypatch.setattr(runtime_module, "get_settings", lambda: settings)
    monkeypatch.setattr(runtime_module, "Database", TrackingDatabase)

    with pytest.raises(RuntimeConfigurationError, match="LLM_MODEL"):
        await runtime_module.init_runtime()

    assert runtime_module._runtime is None
    assert TrackingDatabase.instances == []
