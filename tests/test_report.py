import sqlite3
from datetime import date

import pytest

from rumi_helper.database.db import Database
from rumi_helper.services.github.models import GitHubSnapshot
from rumi_helper.services.notifications import NotificationSettingsService
from rumi_helper.services.report import ReportService


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "report.db"))
    await database.connect()
    yield database
    await database.close()


class FakeDispatcher:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.enqueued: list[tuple[str, str]] = []
        self.wake_calls = 0

    async def send_user(self, user_id: str, message: str):
        self.calls.append((str(user_id), message))

    async def enqueue_user(self, user_id: str, message: str):
        self.enqueued.append((str(user_id), message))
        return 1

    def notify_queue_changed(self):
        self.wake_calls += 1


def _snapshot(sha: str, stars: int) -> GitHubSnapshot:
    return GitHubSnapshot(
        stars=stars,
        forks=0,
        watchers=0,
        commits_count=1,
        latest_commit_sha=sha,
        latest_commit_message=f"commit {sha}",
        latest_commit_author="alice",
        latest_commit_time="2030-01-02T08:00:00Z",
        latest_release=None,
        open_issues_count=0,
    )


async def test_daily_report_aggregates_github_tasks_and_important_memory(db):
    repo_id = await db.insert_github_repository("user-1", "OpenAI", "openai-python", "https://github.com/OpenAI/openai-python")
    old_id = await db.insert_github_snapshot(repo_id, _snapshot("old", 1).to_dict())
    new_id = await db.insert_github_snapshot(repo_id, _snapshot("new", 2).to_dict())
    await db.execute("UPDATE github_snapshots SET captured_at = ? WHERE id = ?", ("2030-01-01 23:00:00", old_id))
    await db.execute("UPDATE github_snapshots SET captured_at = ? WHERE id = ?", ("2030-01-02 08:00:00", new_id))
    memory_id = await db.insert_memory("user-1", "fact", "重要项目上线", 0.9)
    await db.execute("UPDATE memories SET created_at = ?, updated_at = ? WHERE id = ?", ("2030-01-02 09:00:00", "2030-01-02 09:00:00", memory_id))
    task_id = await db.create_scheduled_task(
        "user-1", "reminder", {"message": "提交周报"}, None, "2030-01-02T10:00:00+00:00"
    )
    await db.execute("UPDATE scheduled_tasks SET created_at = ? WHERE id = ?", ("2030-01-02 09:30:00", task_id))

    report = await ReportService(db, None, NotificationSettingsService(db), timezone_name="UTC").build_daily_report(
        "user-1", date(2030, 1, 2)
    )

    assert "【每日总结 2030-01-02】" in report
    assert "OpenAI/openai-python" in report
    assert "commit new" in report
    assert "提交周报" in report
    assert "重要项目上线" in report


async def test_scheduled_daily_report_respects_user_setting_and_sends(db):
    dispatcher = FakeDispatcher()
    notifications = NotificationSettingsService(db)
    await notifications.set("user-1", daily_report=True)
    service = ReportService(db, dispatcher, notifications, timezone_name="UTC")

    await service.run_scheduled_report({"report_date": "2030-01-02"})

    assert dispatcher.enqueued == []
    assert await db.fetchone("SELECT COUNT(*) AS count FROM reports", ()) == {"count": 1}
    outbound = await db.fetchone(
        "SELECT target_type, target_id, status, max_attempts FROM outbound_messages",
        (),
    )
    assert outbound == {
        "target_type": "user",
        "target_id": "user-1",
        "status": "pending",
        "max_attempts": 3,
    }
    assert dispatcher.wake_calls == 1


async def test_scheduled_daily_report_is_idempotent_per_period(db):
    dispatcher = FakeDispatcher()
    notifications = NotificationSettingsService(db)
    await notifications.set("user-1", daily_report=True)
    service = ReportService(
        db,
        dispatcher,
        notifications,
        timezone_name="UTC",
        outbound_max_attempts=7,
    )

    task = {"report_date": "2030-01-02"}
    await service.run_scheduled_report(task)
    await service.run_scheduled_report(task)

    assert await db.fetchone("SELECT COUNT(*) AS count FROM reports", ()) == {"count": 1}
    assert await db.fetchone(
        "SELECT COUNT(*) AS count FROM outbound_messages",
        (),
    ) == {"count": 1}
    assert await db.fetchone(
        "SELECT max_attempts FROM outbound_messages",
        (),
    ) == {"max_attempts": 7}


async def test_report_and_outbound_enqueue_roll_back_together(db):
    dispatcher = FakeDispatcher()
    notifications = NotificationSettingsService(db)
    await notifications.set("user-1", daily_report=True)
    service = ReportService(db, dispatcher, notifications, timezone_name="UTC")

    await db.execute(
        "CREATE TRIGGER reject_report_outbound BEFORE INSERT ON outbound_messages "
        "BEGIN SELECT RAISE(ABORT, 'blocked'); END",
        (),
    )

    with pytest.raises(sqlite3.IntegrityError):
        await service.run_scheduled_report({"report_date": "2030-01-02"})

    assert await db.fetchone("SELECT COUNT(*) AS count FROM reports", ()) == {"count": 0}
    assert await db.fetchone(
        "SELECT COUNT(*) AS count FROM outbound_messages",
        (),
    ) == {"count": 0}
    assert dispatcher.wake_calls == 0


async def test_daily_report_batch_rolls_back_all_users_if_second_enqueue_fails(db):
    dispatcher = FakeDispatcher()
    notifications = NotificationSettingsService(db)
    await notifications.set("user-1", daily_report=True)
    await notifications.set("user-2", daily_report=True)
    service = ReportService(db, dispatcher, notifications, timezone_name="UTC")

    await db.execute(
        "CREATE TRIGGER reject_second_report_outbound BEFORE INSERT ON outbound_messages "
        "WHEN NEW.target_id = 'user-2' "
        "BEGIN SELECT RAISE(ABORT, 'blocked'); END",
        (),
    )

    with pytest.raises(sqlite3.IntegrityError):
        await service.run_scheduled_report({"report_date": "2030-01-02"})

    assert await db.fetchone("SELECT COUNT(*) AS count FROM reports", ()) == {"count": 0}
    assert await db.fetchone(
        "SELECT COUNT(*) AS count FROM outbound_messages",
        (),
    ) == {"count": 0}
    assert dispatcher.wake_calls == 0

    await db.execute("DROP TRIGGER reject_second_report_outbound", ())
    await service.run_scheduled_report({"report_date": "2030-01-02"})

    assert await db.fetchone("SELECT COUNT(*) AS count FROM reports", ()) == {"count": 2}
    assert await db.fetchone(
        "SELECT COUNT(*) AS count FROM outbound_messages",
        (),
    ) == {"count": 2}
    assert dispatcher.wake_calls == 1


async def test_group_report_command_refuses_to_expose_private_report(monkeypatch):
    from types import SimpleNamespace

    from rumi_helper.plugins import report as report_plugin

    calls: list[str] = []
    sent: list[str] = []

    class FakeReport:
        async def build_daily_report(self, user_id: str) -> str:
            calls.append(user_id)
            return "private report body"

    class FakeGroupEvent:
        def __init__(self):
            self.user_id = "20000"

    runtime = SimpleNamespace(report=FakeReport())

    async def fake_send_local_reply(_matcher, _runtime, message: str):
        sent.append(message)

    monkeypatch.setattr(report_plugin, "GroupMessageEvent", FakeGroupEvent)
    monkeypatch.setattr(report_plugin, "get_runtime", lambda: runtime)
    monkeypatch.setattr(report_plugin, "send_local_reply", fake_send_local_reply)

    await report_plugin._handle_report(FakeGroupEvent())

    assert calls == []
    assert sent == ["为了隐私，日报只能在私聊中查看。"]
    assert "private report body" not in sent[0]
