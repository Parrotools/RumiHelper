import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from rumi_helper.database.db import Database
from rumi_helper.plugins.scheduler import parse_notification_command
from rumi_helper.services.notifications import NotificationSettingsService
from rumi_helper.services.scheduler import (
    SchedulerService,
    SchedulerValidationError,
    parse_group_schedule_command,
    parse_joke_schedule_command,
    parse_reminder_command,
)


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "scheduler.db"))
    await database.connect()
    yield database
    await database.close()


class FakeDispatcher:
    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []
        self.enqueued: list[tuple[str, str, str]] = []
        self.wake_calls = 0

    async def send_user(self, user_id: str, message: str):
        self.calls.append(("user", user_id, message))

    async def enqueue_user(self, user_id: str, message: str):
        self.enqueued.append(("user", user_id, message))
        return 1

    async def enqueue_group(self, group_id: str, message: str):
        self.enqueued.append(("group", group_id, message))
        return 1

    def notify_queue_changed(self):
        self.wake_calls += 1


class FakeLLM:
    def __init__(self, responses: list[str]):
        self.responses = responses
        self.calls: list[list[dict]] = []

    async def chat(self, messages: list[dict], *, temperature: float = 0.7, max_tokens: int | None = None):
        self.calls.append(messages)
        return self.responses.pop(0)


def test_parse_one_time_and_cron_reminders():
    run_at, cron_expression, message = parse_reminder_command(
        "/remind 2030-01-02 08:30 -- 记得提交周报"
    )
    assert run_at == datetime(2030, 1, 2, 8, 30, tzinfo=UTC)
    assert cron_expression is None
    assert message == "记得提交周报"

    run_at, cron_expression, message = parse_reminder_command("/remind cron:0 8 * * * -- 早上好")
    assert run_at is None
    assert cron_expression == "0 8 * * *"
    assert message == "早上好"




def test_parse_reminder_rejects_oversized_message():
    with pytest.raises(SchedulerValidationError, match="4000"):
        parse_reminder_command(
            "/remind 2030-01-02 08:30 -- " + ("x" * 4001)
        )


def test_parse_group_schedule_rejects_oversized_message():
    with pytest.raises(SchedulerValidationError, match="4000"):
        parse_group_schedule_command(
            "/schedule group:123456 2030-01-02 08:30 -- " + ("x" * 4001)
        )

def test_parse_reminder_rejects_invalid_format():
    with pytest.raises(SchedulerValidationError):
        parse_reminder_command("/remind tomorrow -- 内容")


def test_parse_group_schedule_command():
    group_id, run_at, cron_expression, message = parse_group_schedule_command(
        "/schedule group:123456 cron:0 8 * * * -- 早上好"
    )
    assert group_id == "123456"
    assert run_at is None
    assert cron_expression == "0 8 * * *"
    assert message == "早上好"

    group_id, run_at, cron_expression, message = parse_group_schedule_command(
        "/schedule group:123456 2030-01-02 08:30 -- 记得开会"
    )
    assert group_id == "123456"
    assert run_at == datetime(2030, 1, 2, 8, 30, tzinfo=UTC)
    assert cron_expression is None
    assert message == "记得开会"


def test_parse_group_schedule_rejects_non_group_target():
    with pytest.raises(SchedulerValidationError, match="group:群号"):
        parse_group_schedule_command("/schedule user:123 cron:0 8 * * * -- 消息")


def test_parse_joke_schedule_command():
    group_id, run_at, cron_expression, topic = parse_joke_schedule_command(
        "/schedule joke group:123456 cron:0 12 * * * -- mobile"
    )
    assert group_id == "123456"
    assert run_at is None
    assert cron_expression == "0 12 * * *"
    assert topic == "mobile"


def test_parse_report_notification_command():
    assert parse_notification_command("/notify report on") == ("daily_report", True)


async def test_notification_settings_default_off_and_can_be_enabled(db):
    settings = NotificationSettingsService(db)

    assert (await settings.get("user-1"))["reminder_notify"] is False
    await settings.set("user-1", reminder_notify=True)
    assert (await settings.get("user-1"))["reminder_notify"] is True


async def test_create_and_run_one_time_reminder(db):
    dispatcher = FakeDispatcher()
    settings = NotificationSettingsService(db)
    await settings.set("user-1", reminder_notify=True)
    scheduler = SchedulerService(db, dispatcher, timezone_name="UTC")

    task_id = await scheduler.create_reminder(
        "user-1", "记得提交周报", datetime(2030, 1, 2, 8, 30, tzinfo=UTC)
    )
    await scheduler.run_task(task_id)

    assert dispatcher.enqueued == [("user", "user-1", "记得提交周报")]
    row = await db.fetchone("SELECT enabled, last_run FROM scheduled_tasks WHERE id = ?", (task_id,))
    assert row["enabled"] == 0
    assert row["last_run"] is not None


async def test_create_and_run_group_message(db):
    dispatcher = FakeDispatcher()
    scheduler = SchedulerService(db, dispatcher, timezone_name="UTC")
    task_id = await scheduler.create_group_message(
        "admin-1", "30000", "早上好", datetime(2030, 1, 2, 8, 30, tzinfo=UTC)
    )

    await scheduler.run_task(task_id)

    assert dispatcher.enqueued == [("group", "30000", "早上好")]
    row = await db.fetchone("SELECT task_type, enabled FROM scheduled_tasks WHERE id = ?", (task_id,))
    assert row["task_type"] == "group_message"
    assert row["enabled"] == 0


async def test_create_and_run_topic_joke_avoids_history(db):
    dispatcher = FakeDispatcher()
    llm = FakeLLM(["昨天的旧段子", "昨天的旧段子", "今天的新段子"])
    scheduler = SchedulerService(db, dispatcher, timezone_name="UTC", llm=llm)
    task_id = await scheduler.create_topic_joke(
        "admin-1", "30000", "mobile", cron_expression="0 12 * * *"
    )
    await db.insert_scheduled_content_history(task_id, "昨天的旧段子")

    await scheduler.run_task(task_id)

    assert dispatcher.enqueued == []
    outbound = await db.fetchone(
        "SELECT target_type, target_id, content, status FROM outbound_messages",
        (),
    )
    assert outbound == {
        "target_type": "group",
        "target_id": "30000",
        "content": "今天的新段子",
        "status": "pending",
    }
    assert dispatcher.wake_calls == 1
    assert len(llm.calls) == 3
    history = await db.fetch_scheduled_content_history(task_id)
    assert history[:2] == ["今天的新段子", "昨天的旧段子"]
    assert "昨天的旧段子" in llm.calls[0][1]["content"]


async def test_topic_joke_history_and_outbox_roll_back_together(db):
    dispatcher = FakeDispatcher()
    llm = FakeLLM(["今天的新段子", "今天的新段子"])
    scheduler = SchedulerService(
        db,
        dispatcher,
        timezone_name="UTC",
        llm=llm,
        outbound_max_attempts=7,
    )
    task_id = await scheduler.create_topic_joke(
        "admin-1",
        "30000",
        "mobile",
        cron_expression="0 12 * * *",
    )
    await db.execute(
        "CREATE TRIGGER reject_topic_joke_outbox BEFORE INSERT ON outbound_messages "
        "BEGIN SELECT RAISE(ABORT, 'blocked'); END",
        (),
    )

    await scheduler.run_task(task_id)

    assert await db.fetchone(
        "SELECT COUNT(*) AS count FROM outbound_messages",
        (),
    ) == {"count": 0}
    assert await db.fetchone(
        "SELECT COUNT(*) AS count FROM scheduled_content_history WHERE task_id = ?",
        (task_id,),
    ) == {"count": 0}
    assert dispatcher.wake_calls == 0

    await db.execute("DROP TRIGGER reject_topic_joke_outbox", ())
    await scheduler.run_task(task_id)

    outbound = await db.fetchone(
        "SELECT target_type, target_id, content, max_attempts, status "
        "FROM outbound_messages",
        (),
    )
    assert outbound == {
        "target_type": "group",
        "target_id": "30000",
        "content": "今天的新段子",
        "max_attempts": 7,
        "status": "pending",
    }
    assert await db.fetch_scheduled_content_history(task_id) == ["今天的新段子"]
    assert dispatcher.wake_calls == 1


async def test_topic_joke_duplicate_history_cannot_queue_twice(db):
    task_id = await db.create_scheduled_task(
        "admin-1",
        "topic_joke",
        {"target_type": "group", "target_id": "30000", "topic": "mobile"},
        "0 12 * * *",
        "2030-01-01T12:00:00+00:00",
    )

    assert await db.record_scheduled_content_and_enqueue_group(
        task_id,
        "30000",
        "同一个段子",
        3,
    ) is True
    assert await db.record_scheduled_content_and_enqueue_group(
        task_id,
        "30000",
        "同一个段子",
        3,
    ) is False

    assert await db.fetchone(
        "SELECT COUNT(*) AS count FROM scheduled_content_history WHERE task_id = ?",
        (task_id,),
    ) == {"count": 1}
    assert await db.fetchone(
        "SELECT COUNT(*) AS count FROM outbound_messages",
        (),
    ) == {"count": 1}


async def test_reminder_does_not_send_when_notifications_are_off(db):
    dispatcher = FakeDispatcher()
    scheduler = SchedulerService(db, dispatcher, timezone_name="UTC")
    task_id = await scheduler.create_reminder(
        "user-1", "不会推送", datetime(2030, 1, 2, 8, 30, tzinfo=UTC)
    )

    await scheduler.run_task(task_id)

    assert dispatcher.calls == []


async def test_scheduler_starts_and_registers_persisted_task(db):
    scheduler = SchedulerService(db, FakeDispatcher(), timezone_name="UTC")
    task_id = await scheduler.create_reminder(
        "user-1", "稍后提醒", datetime(2030, 1, 2, 8, 30, tzinfo=UTC)
    )

    await scheduler.start()
    try:
        assert scheduler._scheduler.get_job(f"scheduled-task-{task_id}") is not None
    finally:
        await scheduler.stop()


async def test_system_task_cron_is_updated_and_can_be_disabled(db):
    async def github_check(_task: dict) -> None:
        return None

    scheduler = SchedulerService(db, FakeDispatcher(), timezone_name="UTC")
    scheduler.register_handler("github_check", github_check)
    await scheduler.create_task("__system__", "github_check", {}, cron_expression="0 * * * *")

    await scheduler.sync_system_task("github_check", "5 * * * *")
    updated = await db.fetch_scheduled_task_by_owner_type("__system__", "github_check")
    assert updated["cron_expression"] == "5 * * * *"
    assert updated["enabled"] == 1

    await scheduler.sync_system_task("github_check", "")
    disabled = await db.fetch_scheduled_task_by_owner_type("__system__", "github_check")
    assert disabled["enabled"] == 0


async def test_system_task_cron_updates_live_scheduler_without_restart(db):
    async def github_check(_task: dict) -> None:
        return None

    scheduler = SchedulerService(db, FakeDispatcher(), timezone_name="UTC")
    scheduler.register_handler("github_check", github_check)
    task_id = await scheduler.create_task(
        "__system__",
        "github_check",
        {},
        cron_expression="0 * * * *",
    )

    await scheduler.start()
    try:
        job_id = scheduler._job_id(task_id)
        old_trigger = str(scheduler._scheduler.get_job(job_id).trigger)

        await scheduler.sync_system_task("github_check", "5 * * * *")
        updated_job = scheduler._scheduler.get_job(job_id)
        assert updated_job is not None
        assert str(updated_job.trigger) != old_trigger
        assert "minute='5'" in str(updated_job.trigger)

        await scheduler.sync_system_task("github_check", "")
        assert scheduler._scheduler.get_job(job_id) is None
    finally:
        await scheduler.stop()


async def test_disabling_running_system_task_is_not_overwritten_by_stale_run(db):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def github_check(_task: dict) -> None:
        entered.set()
        await release.wait()

    scheduler = SchedulerService(db, FakeDispatcher(), timezone_name="UTC")
    scheduler.register_handler("github_check", github_check)
    task_id = await scheduler.create_task(
        "__system__",
        "github_check",
        {},
        cron_expression="0 * * * *",
    )

    running = asyncio.create_task(scheduler.run_task(task_id))
    await entered.wait()

    await scheduler.sync_system_task("github_check", "")
    release.set()
    await running

    task = await db.fetch_scheduled_task(task_id)
    assert task["enabled"] == 0
    assert task["cron_expression"] is None
    assert task["next_run"] is None


async def test_stop_waits_for_inflight_task(db):
    entered = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def slow_task(_task: dict) -> None:
        entered.set()
        await release.wait()
        finished.set()

    scheduler = SchedulerService(db, FakeDispatcher(), timezone_name="UTC")
    scheduler.register_handler("slow", slow_task)
    task_id = await scheduler.create_task(
        "owner",
        "slow",
        {},
        cron_expression="0 * * * *",
    )
    await scheduler.start()
    running = asyncio.create_task(scheduler.run_task(task_id))
    await entered.wait()

    stopping = asyncio.create_task(scheduler.stop())
    await asyncio.sleep(0)
    assert stopping.done() is False

    release.set()
    await stopping
    await running

    assert finished.is_set()
    assert scheduler.is_running is False


async def test_stop_cancels_inflight_task_after_shutdown_timeout(db):
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def stuck_task(_task: dict) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    scheduler = SchedulerService(
        db,
        FakeDispatcher(),
        timezone_name="UTC",
        shutdown_timeout_seconds=0.1,
    )
    scheduler.register_handler("stuck", stuck_task)
    task_id = await scheduler.create_task(
        "owner",
        "stuck",
        {},
        cron_expression="0 * * * *",
    )
    running = asyncio.create_task(scheduler.run_task(task_id))
    await entered.wait()

    await scheduler.stop()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert cancelled.is_set()
    assert running.cancelled()
    assert not scheduler._active_runs


async def test_stop_is_bounded_when_task_swallows_first_cancel(db):
    entered = asyncio.Event()
    swallowed = asyncio.Event()

    async def stubborn_task(_task: dict) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            swallowed.set()
            # Simulate badly behaved code that suppresses the first cancellation.
            await asyncio.Event().wait()

    scheduler = SchedulerService(
        db,
        FakeDispatcher(),
        timezone_name="UTC",
        shutdown_timeout_seconds=0.1,
    )
    scheduler.register_handler("stubborn", stubborn_task)
    task_id = await scheduler.create_task(
        "owner",
        "stubborn",
        {},
        cron_expression="0 * * * *",
    )
    running = asyncio.create_task(scheduler.run_task(task_id))
    await entered.wait()

    await asyncio.wait_for(scheduler.stop(), timeout=0.5)
    assert swallowed.is_set()

    with pytest.raises(asyncio.CancelledError):
        await running


async def test_concurrent_notification_updates_do_not_overwrite_each_other(tmp_path):
    barrier_count = 0
    barrier_ready = asyncio.Event()

    class BarrierSettingsDatabase(Database):
        def __init__(self, path: str):
            super().__init__(path)
            self._sync_first_fetch = True

        async def fetch_notification_settings(self, user_id: str):
            nonlocal barrier_count
            row = await super().fetch_notification_settings(user_id)
            if self._sync_first_fetch:
                self._sync_first_fetch = False
                barrier_count += 1
                if barrier_count >= 2:
                    barrier_ready.set()
                await barrier_ready.wait()
            return row

    path = str(tmp_path / "notification-race.db")
    first_db = BarrierSettingsDatabase(path)
    second_db = BarrierSettingsDatabase(path)
    await first_db.connect()
    await second_db.connect()
    try:
        first = NotificationSettingsService(first_db)
        second = NotificationSettingsService(second_db)

        await asyncio.gather(
            first.set("user-1", github_notify=True),
            second.set("user-1", daily_report=True),
        )
        stored = await first.get("user-1")

        assert stored["github_notify"] is True
        assert stored["daily_report"] is True
        assert stored["reminder_notify"] is False
    finally:
        await first_db.close()
        await second_db.close()


async def test_scheduled_content_history_keeps_only_latest_twenty(db):
    task_id = await db.create_scheduled_task(
        "admin-1",
        "topic_joke",
        {"target_type": "group", "target_id": "30000", "topic": "mobile"},
        "0 12 * * *",
        "2030-01-01T12:00:00+00:00",
    )

    for i in range(25):
        await db.insert_scheduled_content_history(task_id, f"joke-{i}")

    history = await db.fetch_scheduled_content_history(task_id, limit=100)
    assert len(history) == 20
    assert history[0] == "joke-24"
    assert history[-1] == "joke-5"
    assert await db.fetchone(
        "SELECT COUNT(*) AS count FROM scheduled_content_history WHERE task_id = ?",
        (task_id,),
    ) == {"count": 20}


async def test_restart_recovers_recent_one_time_misfire(db):
    scheduler = SchedulerService(db, FakeDispatcher(), timezone_name="UTC")
    run_at = datetime.now(UTC) - timedelta(seconds=30)
    task_id = await db.create_scheduled_task(
        "user-1",
        "reminder",
        {"target_type": "user", "target_id": "user-1", "message": "recent"},
        None,
        run_at.isoformat(),
    )
    task = await db.fetch_scheduled_task(task_id)

    assert scheduler._schedule(task) is True
    assert scheduler._scheduler.get_job(scheduler._job_id(task_id)) is not None


async def test_restart_drops_one_time_misfire_beyond_grace(db):
    scheduler = SchedulerService(db, FakeDispatcher(), timezone_name="UTC")
    run_at = datetime.now(UTC) - timedelta(seconds=120)
    task_id = await db.create_scheduled_task(
        "user-1",
        "reminder",
        {"target_type": "user", "target_id": "user-1", "message": "stale"},
        None,
        run_at.isoformat(),
    )
    task = await db.fetch_scheduled_task(task_id)

    assert scheduler._schedule(task) is False
    assert scheduler._scheduler.get_job(scheduler._job_id(task_id)) is None


async def test_start_preserves_persisted_cron_occurrence_marker(db):
    scheduler = SchedulerService(db, FakeDispatcher(), timezone_name="UTC")
    persisted = "2000-01-01T00:00:00+00:00"
    task_id = await db.create_scheduled_task(
        "admin-1",
        "group_message",
        {"target_type": "group", "target_id": "30000", "message": "hello"},
        "0 * * * *",
        persisted,
    )

    await scheduler.start()
    try:
        restored = await db.fetch_scheduled_task(task_id)
        assert restored["next_run"] == persisted
        assert scheduler._scheduler.get_job(scheduler._job_id(task_id)) is not None
    finally:
        await scheduler.stop()


async def test_new_process_start_does_not_steal_due_occurrence(tmp_path):
    path = str(tmp_path / "scheduler-start-race.db")
    first_db = Database(path)
    second_db = Database(path)
    await first_db.connect()
    await second_db.connect()
    calls: list[str] = []
    try:
        due = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        task_id = await first_db.create_scheduled_task(
            "owner",
            "probe",
            {},
            "0 * * * *",
            due,
        )

        async def handler(_task: dict) -> None:
            calls.append("ran")

        first = SchedulerService(first_db, FakeDispatcher(), timezone_name="UTC")
        second = SchedulerService(second_db, FakeDispatcher(), timezone_name="UTC")
        first.register_handler("probe", handler)
        second.register_handler("probe", handler)

        await second.start()
        try:
            await first.run_task(task_id, force=False)
        finally:
            await second.stop()

        assert calls == ["ran"]
    finally:
        await first_db.close()
        await second_db.close()


async def test_concurrent_processes_claim_same_scheduled_occurrence_once(tmp_path):
    barrier_count = 0
    barrier_ready = asyncio.Event()

    class BarrierDatabase(Database):
        async def fetch_scheduled_task(self, task_id: int):
            nonlocal barrier_count
            row = await super().fetch_scheduled_task(task_id)
            if row is not None:
                barrier_count += 1
                if barrier_count >= 2:
                    barrier_ready.set()
                await barrier_ready.wait()
            return row

    path = str(tmp_path / "scheduler-race.db")
    first_db = BarrierDatabase(path)
    second_db = BarrierDatabase(path)
    await first_db.connect()
    await second_db.connect()
    calls: list[str] = []

    try:
        due = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        task_id = await first_db.create_scheduled_task(
            "owner",
            "probe",
            {},
            "0 * * * *",
            due,
        )

        async def first_handler(_task: dict) -> None:
            calls.append("first")

        async def second_handler(_task: dict) -> None:
            calls.append("second")

        first = SchedulerService(first_db, FakeDispatcher(), timezone_name="UTC")
        second = SchedulerService(second_db, FakeDispatcher(), timezone_name="UTC")
        first.register_handler("probe", first_handler)
        second.register_handler("probe", second_handler)

        await asyncio.gather(
            first.run_task(task_id, force=False),
            second.run_task(task_id, force=False),
        )

        assert len(calls) == 1
        task = await first_db.fetch_scheduled_task(task_id)
        assert task["enabled"] == 1
        assert task["last_run"] is not None
        assert datetime.fromisoformat(task["next_run"]) > datetime.now(UTC)
    finally:
        await first_db.close()
        await second_db.close()


async def test_stale_trigger_cannot_execute_future_database_occurrence(db):
    calls: list[str] = []

    async def handler(_task: dict) -> None:
        calls.append("ran")

    scheduler = SchedulerService(db, FakeDispatcher(), timezone_name="UTC")
    scheduler.register_handler("probe", handler)
    future = datetime.now(UTC) + timedelta(hours=1)
    task_id = await db.create_scheduled_task(
        "owner",
        "probe",
        {},
        "0 * * * *",
        future.isoformat(),
    )

    await scheduler.run_task(task_id, force=False)

    assert calls == []
    untouched = await db.fetch_scheduled_task(task_id)
    assert untouched["last_run"] is None
    assert untouched["next_run"] == future.isoformat()

    await scheduler.run_task(task_id, force=True)

    assert calls == ["ran"]
    claimed = await db.fetch_scheduled_task(task_id)
    assert claimed["last_run"] is not None
    assert claimed["next_run"] != future.isoformat()


async def test_concurrent_processes_create_only_one_active_system_task(tmp_path):
    barrier_count = 0
    barrier_ready = asyncio.Event()

    class BarrierDatabase(Database):
        def __init__(self, path: str):
            super().__init__(path)
            self._sync_first_system_fetch = True

        async def fetch_scheduled_task_by_owner_type(self, owner_id: str, task_type: str):
            nonlocal barrier_count
            row = await super().fetch_scheduled_task_by_owner_type(owner_id, task_type)
            if self._sync_first_system_fetch and owner_id == "__system__":
                self._sync_first_system_fetch = False
                barrier_count += 1
                if barrier_count >= 2:
                    barrier_ready.set()
                await barrier_ready.wait()
            return row

    path = str(tmp_path / "system-task-race.db")
    first_db = BarrierDatabase(path)
    second_db = BarrierDatabase(path)
    await first_db.connect()
    await second_db.connect()
    try:
        async def handler(_task: dict) -> None:
            return None

        first = SchedulerService(first_db, FakeDispatcher(), timezone_name="UTC")
        second = SchedulerService(second_db, FakeDispatcher(), timezone_name="UTC")
        first.register_handler("github_check", handler)
        second.register_handler("github_check", handler)

        await asyncio.gather(
            first.sync_system_task("github_check", "0 * * * *"),
            second.sync_system_task("github_check", "0 * * * *"),
        )

        rows = await first_db.fetchall(
            "SELECT id, enabled FROM scheduled_tasks "
            "WHERE owner_id = '__system__' AND task_type = 'github_check'",
            (),
        )
        assert len(rows) == 1
        assert rows[0]["enabled"] == 1
    finally:
        await first_db.close()
        await second_db.close()


async def test_active_reminder_limit_releases_capacity_after_task_is_disabled(db):
    scheduler = SchedulerService(
        db,
        FakeDispatcher(),
        timezone_name="UTC",
        max_active_reminders_per_user=2,
    )
    first = await scheduler.create_reminder(
        "user-1",
        "first",
        run_at=datetime.now(UTC) + timedelta(hours=1),
    )
    await scheduler.create_reminder(
        "user-1",
        "second",
        run_at=datetime.now(UTC) + timedelta(hours=2),
    )

    with pytest.raises(SchedulerValidationError, match="最多同时保留 2 个"):
        await scheduler.create_reminder(
            "user-1",
            "third",
            run_at=datetime.now(UTC) + timedelta(hours=3),
        )

    await db.update_scheduled_task_schedule(first, None, None, False)
    third = await scheduler.create_reminder(
        "user-1",
        "third",
        run_at=datetime.now(UTC) + timedelta(hours=3),
    )
    assert isinstance(third, int)


async def test_concurrent_processes_cannot_exceed_active_reminder_limit(tmp_path):
    path = str(tmp_path / "reminder-limit-race.db")
    first_db = Database(path)
    second_db = Database(path)
    await first_db.connect()
    await second_db.connect()
    try:
        first = SchedulerService(
            first_db,
            FakeDispatcher(),
            timezone_name="UTC",
            max_active_reminders_per_user=1,
        )
        second = SchedulerService(
            second_db,
            FakeDispatcher(),
            timezone_name="UTC",
            max_active_reminders_per_user=1,
        )
        run_at = datetime.now(UTC) + timedelta(hours=1)

        results = await asyncio.gather(
            first.create_reminder("user-1", "first", run_at=run_at),
            second.create_reminder("user-1", "second", run_at=run_at),
            return_exceptions=True,
        )

        assert sum(isinstance(item, int) for item in results) == 1
        assert sum(isinstance(item, SchedulerValidationError) for item in results) == 1
        assert await first_db.fetchone(
            "SELECT COUNT(*) AS count FROM scheduled_tasks "
            "WHERE owner_id = ? AND task_type = 'reminder' AND enabled = 1",
            ("user-1",),
        ) == {"count": 1}
    finally:
        await first_db.close()
        await second_db.close()


async def test_admin_task_creation_requires_current_admin(db):
    scheduler = SchedulerService(
        db,
        FakeDispatcher(),
        timezone_name="UTC",
        is_admin=lambda user_id: user_id == "admin-1",
    )
    run_at = datetime.now(UTC) + timedelta(hours=1)

    with pytest.raises(SchedulerValidationError, match="仅管理员"):
        await scheduler.create_group_message(
            "user-1",
            "30000",
            "hello",
            run_at=run_at,
        )

    with pytest.raises(SchedulerValidationError, match="仅管理员"):
        await scheduler.create_task(
            "user-1",
            "group_message",
            {"target_type": "group", "target_id": "30000", "message": "hello"},
            run_at=run_at,
        )

    task_id = await scheduler.create_group_message(
        "admin-1",
        "30000",
        "hello",
        run_at=run_at,
    )
    assert isinstance(task_id, int)


async def test_revoked_admin_task_is_disabled_before_execution(db):
    admins = {"admin-1"}
    dispatcher = FakeDispatcher()
    scheduler = SchedulerService(
        db,
        dispatcher,
        timezone_name="UTC",
        is_admin=lambda user_id: user_id in admins,
    )
    task_id = await scheduler.create_group_message(
        "admin-1",
        "30000",
        "hello",
        run_at=datetime.now(UTC) + timedelta(hours=1),
    )

    admins.clear()
    await scheduler.run_task(task_id, force=True)

    assert dispatcher.enqueued == []
    task = await db.fetch_scheduled_task(task_id)
    assert task["enabled"] == 0
    assert task["cron_expression"] is None
    assert task["next_run"] is None


async def test_start_disables_persisted_admin_task_after_permission_revocation(db):
    task_id = await db.create_scheduled_task(
        "former-admin",
        "group_message",
        {"target_type": "group", "target_id": "30000", "message": "hello"},
        "0 * * * *",
        (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    )
    scheduler = SchedulerService(
        db,
        FakeDispatcher(),
        timezone_name="UTC",
        is_admin=lambda _user_id: False,
    )

    await scheduler.start()
    try:
        task = await db.fetch_scheduled_task(task_id)
        assert task["enabled"] == 0
        assert task["cron_expression"] is None
        assert task["next_run"] is None
        assert scheduler._scheduler.get_job(scheduler._job_id(task_id)) is None
    finally:
        await scheduler.stop()


async def test_personal_reminder_is_not_admin_gated(db):
    scheduler = SchedulerService(
        db,
        FakeDispatcher(),
        timezone_name="UTC",
        is_admin=lambda _user_id: False,
    )

    task_id = await scheduler.create_reminder(
        "user-1",
        "personal",
        run_at=datetime.now(UTC) + timedelta(hours=1),
    )

    assert isinstance(task_id, int)


async def test_start_quarantines_corrupt_tasks_and_keeps_valid_jobs(db):
    future = datetime.now(UTC) + timedelta(hours=1)
    unknown_id = await db.create_scheduled_task(
        "owner",
        "unknown_task_type",
        {},
        "0 * * * *",
        future.isoformat(),
    )
    bad_cron_id = await db.create_scheduled_task(
        "user-1",
        "reminder",
        {"target_type": "user", "target_id": "user-1", "message": "bad cron"},
        "not a cron",
        future.isoformat(),
    )
    bad_payload_id = await db.create_scheduled_task(
        "user-1",
        "reminder",
        {"target_type": "user", "target_id": "user-1", "message": "bad payload"},
        None,
        future.isoformat(),
    )
    await db.execute(
        "UPDATE scheduled_tasks SET payload = ? WHERE id = ?",
        ("not-json", bad_payload_id),
    )

    scheduler = SchedulerService(db, FakeDispatcher(), timezone_name="UTC")
    valid_id = await scheduler.create_reminder(
        "user-1",
        "valid",
        run_at=future + timedelta(minutes=1),
    )

    await scheduler.start()
    try:
        for task_id in (unknown_id, bad_cron_id, bad_payload_id):
            task = await db.fetch_scheduled_task(task_id)
            assert task["enabled"] == 0
            assert task["cron_expression"] is None
            assert task["next_run"] is None
            assert scheduler._scheduler.get_job(scheduler._job_id(task_id)) is None

        assert scheduler._scheduler.get_job(scheduler._job_id(valid_id)) is not None
    finally:
        await scheduler.stop()


async def test_run_task_quarantines_corrupt_payload_before_claim(db):
    future = datetime.now(UTC) + timedelta(hours=1)
    task_id = await db.create_scheduled_task(
        "user-1",
        "reminder",
        {"target_type": "user", "target_id": "user-1", "message": "hello"},
        "0 * * * *",
        future.isoformat(),
    )
    await db.execute(
        "UPDATE scheduled_tasks SET payload = ? WHERE id = ?",
        ("[]", task_id),
    )
    scheduler = SchedulerService(db, FakeDispatcher(), timezone_name="UTC")

    await scheduler.run_task(task_id, force=True)

    task = await db.fetch_scheduled_task(task_id)
    assert task["enabled"] == 0
    assert task["last_run"] is None
    assert task["next_run"] is None
