import asyncio
from types import SimpleNamespace

import pytest

from rumi_helper.database.db import Database
from rumi_helper.services.agent.harness import AgentHarness, _parse_plan
from rumi_helper.services.qq.dispatcher import BroadcastReport, SendResult
from rumi_helper.services.session.manager import SessionManager


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "agent.db"))
    await database.connect()
    yield database
    await database.close()


class FakeLLM:
    def __init__(self, response: str):
        self.response = response
        self.calls: list[list[dict]] = []

    async def chat(self, messages: list[dict], **kwargs) -> str:
        self.calls.append(messages)
        return self.response


class FakeGitHub:
    def __init__(self):
        self.added: list[tuple[str, str]] = []
        self.watched: list[tuple[str, str, str]] = []

    async def add_repository(self, user_id: str, url: str) -> dict:
        self.added.append((user_id, url))
        return {"repo_owner": "OpenAI", "repo_name": "openai-python"}

    async def list_repositories(self, user_id: str) -> list[dict]:
        return [{"repo_owner": "OpenAI", "repo_name": "openai-python"}] if user_id == "user-1" else []

    async def watch(self, user_id: str, url: str, target: str) -> None:
        self.watched.append((user_id, url, target))


class FakeMemory:
    def __init__(self):
        self.saved: list[tuple[str, str, str]] = []

    async def save(self, user_id: str, memory_type: str, content: str) -> int:
        self.saved.append((user_id, memory_type, content))
        return 7

    async def list_for_context(self, user_id: str, limit: int = 20) -> list[dict]:
        del user_id, limit
        return []


class FakeNotifications:
    def __init__(self, reminder_notify: bool = False):
        self.values = {
            "reminder_notify": reminder_notify,
            "github_notify": False,
            "daily_report": False,
        }

    async def get(self, user_id: str) -> dict:
        return {"user_id": user_id, **self.values}

    async def set(self, user_id: str, **changes: bool) -> dict:
        del user_id
        self.values.update(changes)
        return self.values


class FakeScheduler:
    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []

    async def create_reminder(self, *args, **kwargs) -> int:
        self.calls.append(("reminder", args, kwargs))
        return 11

    async def create_group_message(self, *args, **kwargs) -> int:
        self.calls.append(("group_message", args, kwargs))
        return 12

    async def create_topic_joke(self, *args, **kwargs) -> int:
        self.calls.append(("topic_joke", args, kwargs))
        return 13

    async def sync_system_task(self, *args) -> None:
        self.calls.append(("sync_system_task", args, {}))


class FakeDispatcher:
    def __init__(self):
        self.calls: list[tuple[list, str]] = []

    async def broadcast(self, targets: list, message: str) -> BroadcastReport:
        self.calls.append((targets, message))
        return BroadcastReport([SendResult(target.type, target.id, True) for target in targets])


class FakeSessions:
    def __init__(self):
        self.cleared: list[str] = []

    async def clear(self, session_key: str) -> None:
        self.cleared.append(session_key)


class FakeHealth:
    def __init__(self):
        self.include_diagnostics_calls: list[bool] = []

    async def check(self, *, include_diagnostics: bool = True) -> dict:
        self.include_diagnostics_calls.append(include_diagnostics)
        result = {
            "uptime_seconds": 10,
            "llm": {"ok": True},
            "database": {"ok": True},
            "github": {"ok": True},
            "scheduler": {"running": True, "jobs": 1},
        }
        if include_diagnostics:
            result["counts"] = {"github_repositories": 1, "memories": 2}
            result["recent_errors"] = []
        return result


class FakeReport:
    async def build_daily_report(self, user_id: str) -> str:
        return f"日报：{user_id}"


def _runtime(db, llm, github):
    return SimpleNamespace(
        db=db,
        llm=llm,
        github=github,
        github_client=SimpleNamespace(),
        memory=FakeMemory(),
        notifications=FakeNotifications(),
        scheduler=FakeScheduler(),
        dispatcher=FakeDispatcher(),
        sessions=FakeSessions(),
        health=FakeHealth(),
        report=FakeReport(),
        permission=SimpleNamespace(is_admin=lambda user_id: user_id == "admin"),
        settings=SimpleNamespace(
            group_shared_context=False,
            scheduler_timezone="UTC",
            max_broadcast_recipients=20,
            github_digest_cron="0 6,18 * * *",
        ),
    )


def _event(user_id: str = "user-1"):
    return SimpleNamespace(user_id=user_id, group_id=None)


def test_parse_plan_accepts_json_code_fence_and_rejects_unknown_tool():
    allowed = frozenset({"github_list_repositories"})

    assert _parse_plan('```json\n{"action":"github_list_repositories","arguments":{}}\n```', allowed) == {
        "action": "github_list_repositories",
        "arguments": {},
    }
    assert _parse_plan('{"action":"run_shell","arguments":{}}', allowed) is None
    assert _parse_plan("not json", allowed) is None


@pytest.mark.asyncio
async def test_mutating_tool_is_pending_until_confirmation(db):
    llm = FakeLLM('{"action":"github_add_repository","arguments":{"url":"OpenAI/openai-python"}}')
    github = FakeGitHub()
    runtime = _runtime(db, llm, github)
    harness = AgentHarness(confirmation_ttl_seconds=300)

    result = await harness.try_handle(runtime=runtime, event=_event(), text="把这个仓库加入我的 GitHub 列表")

    assert result is not None and result.handled
    assert "确认执行" in result.message
    assert github.added == []
    pending = await db.fetchone("SELECT tool_name, status, arguments FROM pending_agent_actions", ())
    assert pending["tool_name"] == "github_add_repository"
    assert pending["status"] == "pending"
    assert "https://github.com/OpenAI/openai-python" in pending["arguments"]

    confirmed = await harness.confirm(runtime=runtime, event=_event())
    assert confirmed == "已添加仓库 OpenAI/openai-python。"
    assert github.added == [("user-1", "https://github.com/OpenAI/openai-python")]
    assert await harness.confirm(runtime=runtime, event=_event()) == "没有待确认的操作（可能已过期、已执行或不存在）。"


@pytest.mark.asyncio
async def test_repo_referent_resolves_latest_user_github_url(db):
    llm = FakeLLM('{"action":"github_add_repository","arguments":{"url":"这个仓库"}}')
    github = FakeGitHub()
    runtime = _runtime(db, llm, github)
    harness = AgentHarness()
    history = [{"role": "user", "content": "帮我总结 https://github.com/Parrotools/RumiHelper"}]

    result = await harness.try_handle(
        runtime=runtime,
        event=_event(),
        text="把这个仓库加入仓库列表里",
        history=history,
    )

    assert result is not None and "确认执行" in result.message
    pending = await db.fetchone("SELECT arguments FROM pending_agent_actions", ())
    assert pending["arguments"] == '{"url":"https://github.com/Parrotools/RumiHelper"}'
    assert await harness.confirm(runtime=runtime, event=_event()) == "已添加仓库 OpenAI/openai-python。"
    assert github.added == [("user-1", "https://github.com/Parrotools/RumiHelper")]


@pytest.mark.asyncio
async def test_read_only_tool_runs_without_confirmation(db):
    llm = FakeLLM('{"action":"github_list_repositories","arguments":{}}')
    github = FakeGitHub()
    runtime = _runtime(db, llm, github)
    harness = AgentHarness()

    result = await harness.try_handle(runtime=runtime, event=_event(), text="看看我的 GitHub 仓库列表")

    assert result is not None and result.handled
    assert result.message == "你的 GitHub 仓库：\nOpenAI/openai-python"
    assert await db.fetchone("SELECT id FROM pending_agent_actions", ()) is None


@pytest.mark.asyncio
async def test_non_request_skips_planner(db):
    llm = FakeLLM('{"action":"none","arguments":{}}')
    runtime = _runtime(db, llm, FakeGitHub())
    harness = AgentHarness()

    result = await harness.try_handle(runtime=runtime, event=_event(), text="今天天气怎么样？")

    assert result is None
    assert llm.calls == []


def test_tool_registry_covers_current_natural_language_operations():
    expected = {
        "github_add_repository", "github_remove_repository", "github_list_repositories", "github_check_repository",
        "github_info_repository", "github_watch_repository", "github_digest_set", "github_digest_list",
        "github_digest_clear", "memory_save", "memory_list", "notification_set", "reminder_create",
        "schedule_group_message", "schedule_topic_joke", "broadcast_message", "clear_session", "status",
        "daily_report",
    }
    assert AgentHarness().tool_names == expected


@pytest.mark.asyncio
async def test_memory_save_is_confirmed_before_writing(db):
    llm = FakeLLM('{"action":"memory_save","arguments":{"content":"我喜欢研究 HPC"}}')
    runtime = _runtime(db, llm, FakeGitHub())
    harness = AgentHarness()

    result = await harness.try_handle(runtime=runtime, event=_event(), text="记住我喜欢研究 HPC")

    assert result is not None and "确认执行" in result.message
    assert runtime.memory.saved == []
    assert await harness.confirm(runtime=runtime, event=_event()) == "已保存长期记忆（fact，编号 7）。"
    assert runtime.memory.saved == [("user-1", "fact", "我喜欢研究 HPC")]


@pytest.mark.asyncio
async def test_notification_and_reminder_use_confirmation_and_existing_settings(db):
    llm = FakeLLM('{"action":"notification_set","arguments":{"name":"reminder","enabled":true}}')
    runtime = _runtime(db, llm, FakeGitHub())
    harness = AgentHarness()

    result = await harness.try_handle(runtime=runtime, event=_event(), text="开启提醒通知")
    assert result is not None and "确认执行" in result.message
    assert runtime.notifications.values["reminder_notify"] is False
    assert await harness.confirm(runtime=runtime, event=_event()) == "提醒通知已开启。"
    assert runtime.notifications.values["reminder_notify"] is True

    runtime.llm = FakeLLM(
        '{"action":"reminder_create","arguments":{"timing":"cron:0 8 * * *","message":"提交周报"}}'
    )
    result = await harness.try_handle(runtime=runtime, event=_event(), text="每天早上 8 点提醒我提交周报")
    assert result is not None and "确认执行" in result.message
    assert runtime.scheduler.calls == []
    assert await harness.confirm(runtime=runtime, event=_event()) == "提醒已创建（编号 11）。"
    assert runtime.scheduler.calls[0][0] == "reminder"


@pytest.mark.asyncio
async def test_admin_schedule_topic_and_broadcast_are_guarded(db):
    runtime = _runtime(
        db,
        FakeLLM(
            '{"action":"schedule_topic_joke","arguments":{"group_id":"456789",'
            '"timing":"cron:0 12 * * *","topic":"mobile"}}'
        ),
        FakeGitHub(),
    )
    harness = AgentHarness()

    denied = await harness.try_handle(
        runtime=runtime, event=_event("user-1"), text="每天中午在群 456789 讲 mobile 段子"
    )
    assert denied is not None and denied.message == "该操作仅管理员可用。"

    result = await harness.try_handle(
        runtime=runtime, event=_event("admin"), text="每天中午在群 456789 讲 mobile 段子"
    )
    assert result is not None and "确认执行" in result.message
    assert await harness.confirm(runtime=runtime, event=_event("admin")) == "主题段子任务已创建（编号 13），目标群：456789。"

    runtime.llm = FakeLLM(
        '{"action":"broadcast_message","arguments":{"targets":"user:123456,group:456789",'
        '"message":"服务器已恢复"}}'
    )
    result = await harness.try_handle(runtime=runtime, event=_event("admin"), text="给 user:123456 和群 456789 发消息：服务器已恢复")
    assert result is not None and "确认执行" in result.message
    assert runtime.dispatcher.calls == []
    assert await harness.confirm(runtime=runtime, event=_event("admin")) == "发送完成\n成功：2\n失败：0"
    assert len(runtime.dispatcher.calls) == 1


@pytest.mark.asyncio
async def test_pending_agent_action_replacement_is_serialized(db):
    async def create(tool_name: str) -> int:
        return await db.create_pending_agent_action(
            "user-1",
            "private:user-1",
            tool_name,
            "{}",
            tool_name,
            "9999-01-01T00:00:00+00:00",
        )

    await asyncio.gather(create("memory_save"), create("clear_session"))

    rows = await db.fetchall(
        "SELECT tool_name, status FROM pending_agent_actions ORDER BY id",
        (),
    )
    assert [row["status"] for row in rows].count("pending") == 1
    assert [row["status"] for row in rows].count("cancelled") == 1


@pytest.mark.asyncio
async def test_executing_agent_action_cannot_be_cancelled(db):
    action_id = await db.create_pending_agent_action(
        "user-1",
        "private:user-1",
        "memory_save",
        "{}",
        "save",
        "9999-01-01T00:00:00+00:00",
    )
    claimed = await db.claim_pending_agent_action(
        "user-1",
        "private:user-1",
        "2000-01-01T00:00:00+00:00",
    )

    assert claimed is not None and claimed["id"] == action_id
    assert await db.finish_pending_agent_action(action_id, "cancelled") is False
    assert await db.finish_pending_agent_action(action_id, "confirmed") is True


@pytest.mark.asyncio
async def test_non_admin_cannot_watch_github_to_another_user(db):
    github = FakeGitHub()
    runtime = _runtime(
        db,
        FakeLLM(
            '{"action":"github_watch_repository","arguments":'
            '{"url":"OpenAI/openai-python","target":"user:20002"}}'
        ),
        github,
    )
    harness = AgentHarness()

    result = await harness.try_handle(
        runtime=runtime,
        event=_event("20001"),
        text="把 OpenAI/openai-python 的更新通知给 QQ 20002",
    )

    assert result is not None
    assert result.message == "该操作仅管理员可用。"
    assert github.watched == []
    assert await db.fetchone(
        "SELECT COUNT(*) AS count FROM pending_agent_actions",
        (),
    ) == {"count": 0}


@pytest.mark.asyncio
async def test_github_watch_confirmation_rechecks_admin_permission(db):
    github = FakeGitHub()
    runtime = _runtime(
        db,
        FakeLLM(
            '{"action":"github_watch_repository","arguments":'
            '{"url":"OpenAI/openai-python","target":"user:20002"}}'
        ),
        github,
    )
    harness = AgentHarness()

    pending = await harness.try_handle(
        runtime=runtime,
        event=_event("admin"),
        text="把 OpenAI/openai-python 的更新通知给 QQ 20002",
    )
    assert pending is not None and "确认执行" in pending.message

    runtime.permission = SimpleNamespace(is_admin=lambda _user_id: False)
    confirmed = await harness.confirm(runtime=runtime, event=_event("admin"))

    assert confirmed == "执行失败：当前账号已不再具备管理员权限"
    assert github.watched == []
    assert await db.fetchone(
        "SELECT status FROM pending_agent_actions ORDER BY id DESC LIMIT 1",
        (),
    ) == {"status": "failed"}


@pytest.mark.asyncio
async def test_agent_status_is_minimal_for_non_admin(db):
    runtime = _runtime(
        db,
        FakeLLM('{"action":"status","arguments":{}}'),
        FakeGitHub(),
    )
    harness = AgentHarness()

    result = await harness.try_handle(
        runtime=runtime,
        event=_event("user-1"),
        text="看看机器人状态",
    )

    assert result is not None
    assert "服务：正常" in result.message
    assert "GitHub 监控" not in result.message
    assert "Memory" not in result.message
    assert runtime.health.include_diagnostics_calls == [False]


@pytest.mark.asyncio
async def test_agent_admin_status_requests_diagnostics(db):
    runtime = _runtime(
        db,
        FakeLLM('{"action":"status","arguments":{}}'),
        FakeGitHub(),
    )
    harness = AgentHarness()

    result = await harness.try_handle(
        runtime=runtime,
        event=_event("admin"),
        text="看看机器人状态",
    )

    assert result is not None
    assert "GitHub 监控：1" in result.message
    assert runtime.health.include_diagnostics_calls == [True]


@pytest.mark.asyncio
async def test_agent_cannot_clear_shared_group_session_as_non_admin(db):
    runtime = _runtime(
        db,
        FakeLLM('{"action":"clear_session","arguments":{}}'),
        FakeGitHub(),
    )
    runtime.settings.group_shared_context = True
    harness = AgentHarness()
    event = SimpleNamespace(user_id="user-1", group_id="30000")

    result = await harness.try_handle(
        runtime=runtime,
        event=event,
        text="清空当前会话",
    )

    assert result is not None
    assert result.message == "该操作仅管理员可用。"
    assert runtime.sessions.cleared == []
    assert await db.fetchone(
        "SELECT COUNT(*) AS count FROM pending_agent_actions",
        (),
    ) == {"count": 0}


@pytest.mark.asyncio
async def test_agent_clear_session_confirmation_waits_for_inflight_turn(db):
    runtime = _runtime(
        db,
        FakeLLM('{"action":"clear_session","arguments":{}}'),
        FakeGitHub(),
    )
    runtime.sessions = SessionManager(db, max_context_messages=10)
    harness = AgentHarness()
    event = _event("user-1")
    session_key = "private:user-1"
    await runtime.sessions.append(session_key, "user", "old context")

    pending = await harness.try_handle(
        runtime=runtime,
        event=event,
        text="清空当前会话",
    )
    assert pending is not None and "确认执行" in pending.message

    async with runtime.sessions.turn(session_key):
        confirming = asyncio.create_task(
            harness.confirm(runtime=runtime, event=event)
        )
        await asyncio.sleep(0)
        assert confirming.done() is False
        assert await runtime.sessions.get_context(session_key) == [
            {"role": "user", "content": "old context"}
        ]

    assert await confirming == "当前会话已清空。"
    assert await runtime.sessions.get_context(session_key) == []


@pytest.mark.asyncio
async def test_recover_executing_agent_actions_only_fails_expired_executions(db):
    expired_id = await db.create_pending_agent_action(
        "user-1",
        "private:user-1",
        "memory_save",
        "{}",
        "expired",
        "2001-01-01T00:00:00+00:00",
    )
    claimed = await db.claim_pending_agent_action(
        "user-1",
        "private:user-1",
        "2000-01-01T00:00:00+00:00",
    )
    assert claimed is not None and claimed["id"] == expired_id

    pending_id = await db.create_pending_agent_action(
        "user-2",
        "private:user-2",
        "memory_save",
        "{}",
        "pending",
        "9999-01-01T00:00:00+00:00",
    )
    fresh_id = await db.create_pending_agent_action(
        "user-3",
        "private:user-3",
        "memory_save",
        "{}",
        "fresh",
        "9999-01-01T00:00:00+00:00",
    )
    fresh_claim = await db.claim_pending_agent_action(
        "user-3",
        "private:user-3",
        "2000-01-01T00:00:00+00:00",
    )
    assert fresh_claim is not None and fresh_claim["id"] == fresh_id

    assert (
        await db.recover_executing_agent_actions(
            "2002-01-01T00:00:00+00:00"
        )
        == 1
    )

    rows = await db.fetchall(
        "SELECT id, status FROM pending_agent_actions ORDER BY id",
        (),
    )
    assert rows == [
        {"id": expired_id, "status": "failed"},
        {"id": pending_id, "status": "pending"},
        {"id": fresh_id, "status": "executing"},
    ]
