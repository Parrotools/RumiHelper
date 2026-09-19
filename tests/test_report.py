import asyncio
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
    assert await db.fetchone(
        "SELECT content FROM reports",
        (),
    ) == {"content": ""}
    outbound_body = await db.fetchone(
        "SELECT content FROM outbound_messages",
        (),
    )
    assert outbound_body is not None
    assert "【每日总结 2030-01-02】" in outbound_body["content"]
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


async def test_concurrent_processes_queue_daily_report_once(tmp_path):
    path = str(tmp_path / "report-concurrency.db")
    first_db = Database(path)
    second_db = Database(path)
    await first_db.connect()
    await second_db.connect()
    first_dispatcher = FakeDispatcher()
    second_dispatcher = FakeDispatcher()
    barrier_count = 0
    barrier_ready = asyncio.Event()

    class BarrierReportService(ReportService):
        async def build_daily_report(self, owner_id: str, report_date=None) -> str:
            nonlocal barrier_count
            barrier_count += 1
            if barrier_count >= 2:
                barrier_ready.set()
            await barrier_ready.wait()
            return await super().build_daily_report(owner_id, report_date)

    try:
        first_notifications = NotificationSettingsService(first_db)
        second_notifications = NotificationSettingsService(second_db)
        await first_notifications.set("user-1", daily_report=True)

        first = BarrierReportService(
            first_db,
            first_dispatcher,
            first_notifications,
            timezone_name="UTC",
        )
        second = BarrierReportService(
            second_db,
            second_dispatcher,
            second_notifications,
            timezone_name="UTC",
        )
        task = {"report_date": "2030-01-02"}

        await asyncio.gather(
            first.run_scheduled_report(task),
            second.run_scheduled_report(task),
        )

        assert await first_db.fetchone(
            "SELECT COUNT(*) AS count FROM reports",
            (),
        ) == {"count": 1}
        assert await first_db.fetchone(
            "SELECT COUNT(*) AS count FROM outbound_messages",
            (),
        ) == {"count": 1}
        assert first_dispatcher.wake_calls + second_dispatcher.wake_calls == 1
    finally:
        await first_db.close()
        await second_db.close()


async def test_report_disable_committed_before_enqueue_prevents_delivery(tmp_path):
    path = str(tmp_path / "report-disable-race.db")
    settings_db = Database(path)
    report_db = Database(path)
    await settings_db.connect()
    await report_db.connect()
    try:
        notifications = NotificationSettingsService(settings_db)
        await notifications.set("user-1", daily_report=True)

        report = {
            "owner_id": "user-1",
            "report_type": "daily",
            "period_start": "2030-01-02 00:00:00",
            "period_end": "2030-01-03 00:00:00",
            "content": "ignored",
            "delivery_content": "daily body",
        }

        async with settings_db.operation():
            await settings_db.conn.execute("BEGIN IMMEDIATE")
            await settings_db.conn.execute(
                "UPDATE notification_settings SET daily_report = 0 WHERE user_id = ?",
                ("user-1",),
            )
            saving = asyncio.create_task(
                report_db.save_reports_and_enqueue_users([report], 3)
            )
            await asyncio.sleep(0.05)
            assert saving.done() is False
            await settings_db.conn.commit()

        assert await saving == 0
        assert await report_db.fetchone(
            "SELECT COUNT(*) AS count FROM reports",
            (),
        ) == {"count": 0}
        assert await report_db.fetchone(
            "SELECT COUNT(*) AS count FROM outbound_messages",
            (),
        ) == {"count": 0}
    finally:
        await settings_db.close()
        await report_db.close()


async def test_concurrent_report_services_enqueue_period_once(tmp_path):
    path = str(tmp_path / "report-race.db")
    first_db = Database(path)
    second_db = Database(path)
    await first_db.connect()
    await second_db.connect()
    barrier_count = 0
    barrier_ready = asyncio.Event()

    class BarrierReportService(ReportService):
        async def build_daily_report(self, owner_id: str, report_date: date | None = None) -> str:
            nonlocal barrier_count
            content = await super().build_daily_report(owner_id, report_date)
            barrier_count += 1
            if barrier_count >= 2:
                barrier_ready.set()
            await barrier_ready.wait()
            return content

    try:
        await NotificationSettingsService(first_db).set("user-1", daily_report=True)
        first_dispatcher = FakeDispatcher()
        second_dispatcher = FakeDispatcher()
        first = BarrierReportService(
            first_db,
            first_dispatcher,
            NotificationSettingsService(first_db),
            timezone_name="UTC",
        )
        second = BarrierReportService(
            second_db,
            second_dispatcher,
            NotificationSettingsService(second_db),
            timezone_name="UTC",
        )
        task = {"report_date": "2030-01-02"}

        await asyncio.gather(
            first.run_scheduled_report(task),
            second.run_scheduled_report(task),
        )

        assert await first_db.fetchone(
            "SELECT COUNT(*) AS count FROM reports",
            (),
        ) == {"count": 1}
        assert await first_db.fetchone(
            "SELECT COUNT(*) AS count FROM outbound_messages",
            (),
        ) == {"count": 1}
        assert first_dispatcher.wake_calls + second_dispatcher.wake_calls == 1
    finally:
        await first_db.close()
        await second_db.close()
