import asyncio
import sqlite3
from dataclasses import replace
from datetime import UTC

import httpx
import pytest

import rumi_helper.database.repositories.github as github_repository_module
from rumi_helper.database.db import Database
from rumi_helper.services.github.client import (
    GitHubAPIError,
    GitHubClient,
    GitHubRateLimitError,
    parse_snapshot_payload,
)
from rumi_helper.services.github.tracker import (
    GitHubTracker,
    GitHubTrackerError,
    compare_snapshots,
    format_check_result,
    format_github_digest,
    parse_repo_url,
)
from rumi_helper.services.notifications import NotificationSettingsService
from rumi_helper.utils import QQ_TEXT_SAFE_LIMIT


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "github.db"))
    await database.connect()
    yield database
    await database.close()


def test_parse_github_repository_url():
    ref = parse_repo_url("https://github.com/OpenAI/openai-python/")

    assert ref.owner == "OpenAI"
    assert ref.name == "openai-python"
    assert ref.url == "https://github.com/OpenAI/openai-python"


def test_parse_repo_url_rejects_non_github_url():
    with pytest.raises(GitHubTrackerError):
        parse_repo_url("https://example.com/OpenAI/openai-python")


def test_parse_repo_url_accepts_markdown_link():
    ref = parse_repo_url("[https://github.com/AstrBotDevs/AstrBot.git](https://github.com/AstrBotDevs/AstrBot.git)")

    assert ref.owner == "AstrBotDevs"
    assert ref.name == "AstrBot"
    assert ref.url == "https://github.com/AstrBotDevs/AstrBot"


def test_parse_github_api_payload():
    snapshot = parse_snapshot_payload(
        {
            "stargazers_count": 10,
            "forks_count": 2,
            "subscribers_count": 3,
            "watchers_count": 99,
            "open_issues_count": 4,
        },
        {
            "sha": "abc123",
            "commit": {
                "message": "Improve kernel",
                "author": {"name": "Alice", "date": "2030-01-02T08:30:00Z"},
            },
            "author": {"login": "alice"},
        },
        {"tag_name": "v1.2.0"},
        commits_count=42,
    )

    assert snapshot.stars == 10
    assert snapshot.forks == 2
    assert snapshot.watchers == 3
    assert snapshot.commits_count == 42
    assert snapshot.latest_commit_sha == "abc123"
    assert snapshot.latest_commit_message == "Improve kernel"
    assert snapshot.latest_commit_author == "alice"
    assert snapshot.latest_release == "v1.2.0"


def test_compare_snapshots_detects_commit_and_growth():
    old = {
        "stars": 10,
        "forks": 2,
        "watchers": 3,
        "commits_count": 40,
        "latest_commit_sha": "old",
        "latest_commit_message": "Old commit",
        "latest_commit_author": "alice",
        "latest_commit_time": "2030-01-01T00:00:00Z",
        "latest_release": "v1.1.0",
        "open_issues_count": 1,
    }
    new = {
        **old,
        "stars": 12,
        "forks": 3,
        "commits_count": 42,
        "latest_commit_sha": "new",
        "latest_commit_message": "New commit",
        "latest_release": "v1.2.0",
        "open_issues_count": 2,
    }

    changes = compare_snapshots(old, new)

    assert {change["type"] for change in changes} == {"commit", "stars", "forks", "release", "issues"}


async def test_github_client_reports_rate_limit():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            headers={"X-RateLimit-Remaining": "0"},
            json={"message": "API rate limit exceeded"},
        )

    client = GitHubClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(GitHubRateLimitError):
            await client.get_repository("OpenAI", "openai-python")
    finally:
        await client.aclose()


async def test_github_http_error_does_not_echo_response_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="super-secret-github-echo")

    client = GitHubClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(GitHubAPIError) as exc_info:
            await client.get_repository("OpenAI", "openai-python")
        assert str(exc_info.value) == "GitHub API HTTP 500"
        assert "super-secret-github-echo" not in str(exc_info.value)
    finally:
        await client.aclose()


async def test_github_client_wraps_invalid_json_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    client = GitHubClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(GitHubAPIError, match="响应格式无效"):
            await client.get_repository("OpenAI", "openai-python")
    finally:
        await client.aclose()


async def test_github_client_wraps_invalid_commits_json_response():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits"):
            return httpx.Response(200, content=b"not json")
        return httpx.Response(200, json={})

    client = GitHubClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(GitHubAPIError, match="响应格式无效"):
            await client.fetch_snapshot(parse_repo_url("https://github.com/OpenAI/openai-python"))
    finally:
        await client.aclose()


class FakeGitHubClient:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls: list[tuple[str, str]] = []

    async def fetch_snapshot(self, ref):
        self.calls.append((str(ref.owner), str(ref.name)))
        return self.snapshot


def _snapshot(sha: str, stars: int = 1):
    from rumi_helper.services.github.models import GitHubSnapshot

    return GitHubSnapshot(
        stars=stars,
        forks=0,
        watchers=0,
        commits_count=1,
        latest_commit_sha=sha,
        latest_commit_message="message",
        latest_commit_author="author",
        latest_commit_time="2030-01-01T00:00:00Z",
        latest_release=None,
        open_issues_count=0,
    )


async def test_tracker_isolates_repositories_and_records_snapshots(db):
    client = FakeGitHubClient(_snapshot("first"))
    tracker = GitHubTracker(db, client)

    user_repo = await tracker.add_repository("user-1", "https://github.com/OpenAI/openai-python")
    await tracker.add_repository("user-2", "https://github.com/OpenAI/openai-python")
    first_check = await tracker.check("user-1", user_repo["repo_url"])

    client.snapshot = _snapshot("second", stars=2)
    second_check = await tracker.check("user-1", user_repo["repo_url"])

    assert len(await tracker.list_repositories("user-1")) == 1
    assert first_check["changes"] == []
    assert {change["type"] for change in second_check["changes"]} == {"commit", "stars"}
    assert await db.fetchone("SELECT COUNT(*) AS count FROM github_snapshots", ()) == {"count": 2}


async def test_identical_github_state_does_not_append_duplicate_snapshots(db):
    client = FakeGitHubClient(_snapshot("same"))
    dispatcher = FakeDispatcher()
    tracker = GitHubTracker(db, client, dispatcher)
    repo = await tracker.add_repository(
        "user-1",
        "https://github.com/OpenAI/openai-python",
    )

    await tracker.check("user-1", repo["repo_url"])
    await tracker.check("user-1", repo["repo_url"])
    await tracker.run_scheduled_check({})

    assert await db.fetchone(
        "SELECT COUNT(*) AS count FROM github_snapshots",
        (),
    ) == {"count": 1}
    assert await db.fetchone(
        "SELECT COUNT(*) AS count FROM outbound_messages",
        (),
    ) == {"count": 0}
    assert dispatcher.wake_calls == 0


async def test_non_notification_snapshot_state_change_is_still_persisted(db):
    baseline = _snapshot("same")
    client = FakeGitHubClient(baseline)
    tracker = GitHubTracker(db, client)
    repo = await tracker.add_repository(
        "user-1",
        "https://github.com/OpenAI/openai-python",
    )
    await tracker.check("user-1", repo["repo_url"])

    client.snapshot = replace(baseline, watchers=7)
    result = await tracker.check("user-1", repo["repo_url"])

    assert result["changes"] == []
    assert await db.fetchone(
        "SELECT COUNT(*) AS count FROM github_snapshots",
        (),
    ) == {"count": 2}
    latest = await db.fetch_latest_github_snapshot(repo["id"])
    assert latest is not None and latest["watchers"] == 7


async def test_tracker_prevents_watching_another_users_repository(db):
    tracker = GitHubTracker(db, FakeGitHubClient(_snapshot("first")))
    await tracker.add_repository("user-1", "https://github.com/OpenAI/openai-python")

    with pytest.raises(GitHubTrackerError):
        await tracker.watch("user-2", "https://github.com/OpenAI/openai-python", "group:123")


async def test_remove_repository_cleans_snapshots_and_notifications(db):
    tracker = GitHubTracker(db, FakeGitHubClient(_snapshot("first")))
    repo = await tracker.add_repository("user-1", "https://github.com/OpenAI/openai-python")
    await tracker.watch("user-1", repo["repo_url"], "user:999")
    await tracker.check("user-1", repo["repo_url"])

    assert await tracker.remove_repository("user-1", repo["repo_url"])
    assert await db.fetchone("SELECT COUNT(*) AS count FROM github_snapshots", ()) == {"count": 0}
    assert await db.fetchone("SELECT COUNT(*) AS count FROM github_notifications", ()) == {"count": 0}


class FakeDispatcher:
    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []
        self.enqueued: list[tuple[str, str, str]] = []
        self.wake_calls = 0

    async def send_user(self, user_id: str, message: str):
        self.calls.append(("user", user_id, message))

    async def send_group(self, group_id: str, message: str):
        self.calls.append(("group", group_id, message))

    async def enqueue_user(self, user_id: str, message: str):
        self.enqueued.append(("user", user_id, message))
        return 1

    async def enqueue_group(self, group_id: str, message: str):
        self.enqueued.append(("group", group_id, message))
        return 1

    def notify_queue_changed(self):
        self.wake_calls += 1


async def test_scheduled_check_reuses_one_network_snapshot_for_duplicate_repositories(db):
    client = FakeGitHubClient(_snapshot("same"))
    dispatcher = FakeDispatcher()
    tracker = GitHubTracker(db, client, dispatcher)

    await tracker.add_repository(
        "user-1",
        "https://github.com/OpenAI/openai-python",
    )
    await tracker.add_repository(
        "user-2",
        "https://github.com/OpenAI/openai-python",
    )

    await tracker.run_scheduled_check({})

    assert client.calls == [("OpenAI", "openai-python")]
    rows = await db.fetchall(
        "SELECT repo_id, latest_commit_sha FROM github_snapshots ORDER BY repo_id",
        (),
    )
    assert len(rows) == 2
    assert {row["latest_commit_sha"] for row in rows} == {"same"}


async def test_scheduled_check_sends_only_when_github_notifications_are_enabled(db):
    client = FakeGitHubClient(_snapshot("first"))
    dispatcher = FakeDispatcher()
    notifications = NotificationSettingsService(db)
    tracker = GitHubTracker(db, client, dispatcher, notifications)
    repo = await tracker.add_repository("user-1", "https://github.com/OpenAI/openai-python")
    await tracker.watch("user-1", repo["repo_url"], "user:999")
    await tracker.check("user-1", repo["repo_url"])

    client.snapshot = _snapshot("second", stars=2)
    await tracker.run_scheduled_check({})
    assert dispatcher.calls == []
    assert dispatcher.wake_calls == 0
    assert await db.fetchone(
        "SELECT COUNT(*) AS count FROM outbound_messages",
        (),
    ) == {"count": 0}

    await notifications.set("user-1", github_notify=True)
    client.snapshot = _snapshot("third", stars=3)
    await tracker.run_scheduled_check({})

    outbound = await db.fetchone(
        "SELECT target_type, target_id, content, max_attempts, status FROM outbound_messages",
        (),
    )
    assert outbound is not None
    assert outbound["target_type"] == "user"
    assert outbound["target_id"] == "999"
    assert outbound["max_attempts"] == 3
    assert outbound["status"] == "pending"
    assert "OpenAI/openai-python" in outbound["content"]
    assert dispatcher.wake_calls == 1


async def test_scheduled_check_rolls_back_snapshot_if_outbox_insert_fails(db):
    client = FakeGitHubClient(_snapshot("first"))
    dispatcher = FakeDispatcher()
    notifications = NotificationSettingsService(db)
    await notifications.set("user-1", github_notify=True)
    tracker = GitHubTracker(
        db,
        client,
        dispatcher,
        notifications,
        outbound_max_attempts=7,
    )
    repo = await tracker.add_repository("user-1", "https://github.com/OpenAI/openai-python")
    await tracker.watch("user-1", repo["repo_url"], "user:999")
    await tracker.check("user-1", repo["repo_url"])

    await db.execute(
        "CREATE TRIGGER reject_github_outbox BEFORE INSERT ON outbound_messages "
        "BEGIN SELECT RAISE(ABORT, 'blocked'); END",
        (),
    )
    client.snapshot = _snapshot("second", stars=2)
    await tracker.run_scheduled_check({})

    latest = await db.fetch_latest_github_snapshot(repo["id"])
    assert latest is not None and latest["latest_commit_sha"] == "first"
    assert await db.fetchone(
        "SELECT COUNT(*) AS count FROM outbound_messages",
        (),
    ) == {"count": 0}
    assert dispatcher.wake_calls == 0

    await db.execute("DROP TRIGGER reject_github_outbox", ())
    await tracker.run_scheduled_check({})

    latest = await db.fetch_latest_github_snapshot(repo["id"])
    assert latest is not None and latest["latest_commit_sha"] == "second"
    outbound = await db.fetchone(
        "SELECT target_type, target_id, max_attempts, status FROM outbound_messages",
        (),
    )
    assert outbound == {
        "target_type": "user",
        "target_id": "999",
        "max_attempts": 7,
        "status": "pending",
    }
    assert dispatcher.wake_calls == 1


async def test_concurrent_scheduled_checks_queue_change_only_once(tmp_path):
    barrier_count = 0
    barrier_ready = asyncio.Event()

    class BarrierDatabase(Database):
        sync_latest = False

        async def fetch_latest_github_snapshot(self, repo_id: int):
            nonlocal barrier_count
            row = await super().fetch_latest_github_snapshot(repo_id)
            if self.sync_latest:
                barrier_count += 1
                if barrier_count >= 2:
                    barrier_ready.set()
                await barrier_ready.wait()
            return row

    path = str(tmp_path / "github-race.db")
    first_db = BarrierDatabase(path)
    second_db = BarrierDatabase(path)
    await first_db.connect()
    await second_db.connect()
    try:
        initial = GitHubTracker(first_db, FakeGitHubClient(_snapshot("first")))
        repo = await initial.add_repository(
            "user-1",
            "https://github.com/OpenAI/openai-python",
        )
        await first_db.insert_github_snapshot(repo["id"], _snapshot("first").to_dict())
        await first_db.upsert_github_notification(
            repo["id"],
            "user",
            "user-1",
            True,
        )
        await NotificationSettingsService(first_db).set(
            "user-1",
            github_notify=True,
        )

        first_db.sync_latest = True
        second_db.sync_latest = True
        first_dispatcher = FakeDispatcher()
        second_dispatcher = FakeDispatcher()
        first_tracker = GitHubTracker(
            first_db,
            FakeGitHubClient(_snapshot("second", stars=2)),
            first_dispatcher,
            NotificationSettingsService(first_db),
        )
        second_tracker = GitHubTracker(
            second_db,
            FakeGitHubClient(_snapshot("second", stars=2)),
            second_dispatcher,
            NotificationSettingsService(second_db),
        )

        await asyncio.gather(
            first_tracker.run_scheduled_check({}),
            second_tracker.run_scheduled_check({}),
        )

        assert await first_db.fetchone(
            "SELECT COUNT(*) AS count FROM github_snapshots",
            (),
        ) == {"count": 2}
        assert await first_db.fetchone(
            "SELECT COUNT(*) AS count FROM outbound_messages",
            (),
        ) == {"count": 1}
        assert first_dispatcher.wake_calls + second_dispatcher.wake_calls == 1
    finally:
        await first_db.close()
        await second_db.close()




async def test_concurrent_scheduled_checks_without_notifications_store_snapshot_once(tmp_path):
    barrier_count = 0
    barrier_ready = asyncio.Event()

    class BarrierDatabase(Database):
        sync_latest = False

        async def fetch_latest_github_snapshot(self, repo_id: int):
            nonlocal barrier_count
            row = await super().fetch_latest_github_snapshot(repo_id)
            if self.sync_latest:
                barrier_count += 1
                if barrier_count >= 2:
                    barrier_ready.set()
                await barrier_ready.wait()
            return row

    path = str(tmp_path / "github-race-no-notify.db")
    first_db = BarrierDatabase(path)
    second_db = BarrierDatabase(path)
    await first_db.connect()
    await second_db.connect()
    try:
        initial = GitHubTracker(first_db, FakeGitHubClient(_snapshot("first")))
        repo = await initial.add_repository(
            "user-1",
            "https://github.com/OpenAI/openai-python",
        )
        await first_db.insert_github_snapshot(repo["id"], _snapshot("first").to_dict())

        first_db.sync_latest = True
        second_db.sync_latest = True
        first_dispatcher = FakeDispatcher()
        second_dispatcher = FakeDispatcher()
        first_tracker = GitHubTracker(
            first_db,
            FakeGitHubClient(_snapshot("second", stars=2)),
            first_dispatcher,
            NotificationSettingsService(first_db),
        )
        second_tracker = GitHubTracker(
            second_db,
            FakeGitHubClient(_snapshot("second", stars=2)),
            second_dispatcher,
            NotificationSettingsService(second_db),
        )

        await asyncio.gather(
            first_tracker.run_scheduled_check({}),
            second_tracker.run_scheduled_check({}),
        )

        assert await first_db.fetchone(
            "SELECT COUNT(*) AS count FROM github_snapshots",
            (),
        ) == {"count": 2}
        assert await first_db.fetchone(
            "SELECT COUNT(*) AS count FROM outbound_messages",
            (),
        ) == {"count": 0}
        assert first_dispatcher.wake_calls + second_dispatcher.wake_calls == 0
    finally:
        await first_db.close()
        await second_db.close()


async def test_scheduled_digest_sends_latest_commit_to_all_configured_users(db):
    dispatcher = FakeDispatcher()
    tracker = GitHubTracker(
        db,
        FakeGitHubClient(_snapshot("latest")),
        dispatcher,
        timezone_name="UTC",
    )
    await tracker.add_repository("user-1", "https://github.com/OpenAI/openai-python")
    await db.replace_github_digest_targets([
        {"target_type": "user", "target_id": "999"},
        {"target_type": "group", "target_id": "888"},
        {"target_type": "user", "target_id": "999"},
    ])

    await tracker.run_scheduled_digest({})

    outbound = await db.fetchall(
        "SELECT target_type, target_id, content, status FROM outbound_messages ORDER BY id",
        (),
    )
    assert [(row["target_type"], row["target_id"]) for row in outbound] == [
        ("group", "888"),
        ("user", "999"),
    ]
    assert outbound[0]["content"] == outbound[1]["content"]
    assert outbound[0]["status"] == outbound[1]["status"] == "pending"
    assert "Last commit 时间：2030-01-01 00:00:00" in outbound[0]["content"]
    assert "Last commit 消息：\n  message" in outbound[0]["content"]
    assert dispatcher.wake_calls == 1


async def test_scheduled_digest_rolls_back_entire_recipient_batch_on_enqueue_failure(db):
    dispatcher = FakeDispatcher()
    tracker = GitHubTracker(
        db,
        FakeGitHubClient(_snapshot("latest")),
        dispatcher,
        timezone_name="UTC",
    )
    await tracker.add_repository("user-1", "https://github.com/OpenAI/openai-python")
    await db.replace_github_digest_targets([
        {"target_type": "group", "target_id": "888"},
        {"target_type": "user", "target_id": "999"},
    ])
    await db.execute(
        "CREATE TRIGGER reject_second_digest_target BEFORE INSERT ON outbound_messages "
        "WHEN NEW.target_id = '999' "
        "BEGIN SELECT RAISE(ABORT, 'blocked'); END",
        (),
    )

    await tracker.run_scheduled_digest({})

    assert await db.fetchone(
        "SELECT COUNT(*) AS count FROM outbound_messages",
        (),
    ) == {"count": 0}
    assert dispatcher.wake_calls == 0

    await db.execute("DROP TRIGGER reject_second_digest_target", ())
    await tracker.run_scheduled_digest({})

    assert await db.fetchone(
        "SELECT COUNT(*) AS count FROM outbound_messages",
        (),
    ) == {"count": 2}
    assert dispatcher.wake_calls == 1


def test_format_github_digest_handles_empty_repository_list():
    assert "暂无已登记的 GitHub 仓库" in format_github_digest([], timezone_info=UTC)


def test_parse_github_command():
    from rumi_helper.plugins.github import parse_github_command, parse_github_digest_command

    assert parse_github_command("/github add https://github.com/OpenAI/openai-python") == (
        "add",
        ["https://github.com/OpenAI/openai-python"],
    )
    assert parse_github_digest_command(["set", "user:123,group:456"]) == (
        "set",
        [{"target_type": "user", "target_id": "123"}, {"target_type": "group", "target_id": "456"}],
    )
    with pytest.raises(GitHubTrackerError, match="超过上限"):
        parse_github_digest_command(
            ["set", "user:1,user:2,user:3"],
            max_targets=2,
        )


async def test_replace_digest_targets_rolls_back_on_invalid_target(db):
    await db.replace_github_digest_targets(
        [{"target_type": "user", "target_id": "123"}]
    )

    with pytest.raises(sqlite3.IntegrityError):
        await db.replace_github_digest_targets(
            [{"target_type": "invalid", "target_id": "999"}]
        )

    assert await db.fetch_github_digest_targets() == [
        {"target_type": "user", "target_id": "123"}
    ]


async def test_delete_repository_rolls_back_all_related_rows_on_failure(db):
    tracker = GitHubTracker(db, FakeGitHubClient(_snapshot("first")))
    repo = await tracker.add_repository(
        "user-1",
        "https://github.com/OpenAI/openai-python",
    )
    await tracker.watch("user-1", repo["repo_url"], "user:999")
    await tracker.check("user-1", repo["repo_url"])

    await db.execute(
        "CREATE TRIGGER prevent_repo_delete BEFORE DELETE ON github_repositories "
        "BEGIN SELECT RAISE(ABORT, 'blocked'); END",
        (),
    )

    with pytest.raises(sqlite3.IntegrityError):
        await db.delete_github_repository("user-1", "OpenAI", "openai-python")

    assert await db.fetchone(
        "SELECT COUNT(*) AS count FROM github_repositories",
        (),
    ) == {"count": 1}
    assert await db.fetchone(
        "SELECT COUNT(*) AS count FROM github_snapshots",
        (),
    ) == {"count": 1}
    assert await db.fetchone(
        "SELECT COUNT(*) AS count FROM github_notifications",
        (),
    ) == {"count": 1}


async def test_concurrent_checks_of_same_repo_use_latest_snapshot(db):
    client = FakeGitHubClient(_snapshot("baseline", stars=1))
    tracker = GitHubTracker(db, client)
    repo = await tracker.add_repository(
        "user-1",
        "https://github.com/OpenAI/openai-python",
    )
    await tracker.check("user-1", repo["repo_url"])

    client.snapshot = _snapshot("updated", stars=2)
    first, second = await asyncio.gather(
        tracker.check("user-1", repo["repo_url"]),
        tracker.check("user-1", repo["repo_url"]),
    )

    changed = [bool(result["changes"]) for result in (first, second)]
    assert changed.count(True) == 1
    assert changed.count(False) == 1


async def test_github_repository_limit_allows_upsert_but_rejects_new_repo(db):
    tracker = GitHubTracker(
        db,
        FakeGitHubClient(_snapshot("first")),
        max_repositories_per_user=2,
    )

    await tracker.add_repository("user-1", "https://github.com/OpenAI/openai-python")
    await tracker.add_repository("user-1", "https://github.com/pallets/flask")

    # Re-adding an existing repository is an upsert and must not consume quota.
    existing = await tracker.add_repository(
        "user-1",
        "https://github.com/OpenAI/openai-python/",
    )
    assert existing["repo_name"] == "openai-python"

    with pytest.raises(GitHubTrackerError, match="最多监控 2 个"):
        await tracker.add_repository("user-1", "https://github.com/psf/requests")

    assert len(await tracker.list_repositories("user-1")) == 2


async def test_concurrent_processes_cannot_exceed_github_repository_limit(tmp_path):
    path = str(tmp_path / "github-repo-limit-race.db")
    first_db = Database(path)
    second_db = Database(path)
    await first_db.connect()
    await second_db.connect()
    try:
        first = GitHubTracker(
            first_db,
            FakeGitHubClient(_snapshot("first")),
            max_repositories_per_user=1,
        )
        second = GitHubTracker(
            second_db,
            FakeGitHubClient(_snapshot("first")),
            max_repositories_per_user=1,
        )

        results = await asyncio.gather(
            first.add_repository("user-1", "https://github.com/OpenAI/openai-python"),
            second.add_repository("user-1", "https://github.com/pallets/flask"),
            return_exceptions=True,
        )

        assert sum(isinstance(item, dict) for item in results) == 1
        assert sum(isinstance(item, GitHubTrackerError) for item in results) == 1
        assert await first_db.fetchone(
            "SELECT COUNT(*) AS count FROM github_repositories WHERE owner_id = ?",
            ("user-1",),
        ) == {"count": 1}
    finally:
        await first_db.close()
        await second_db.close()


def test_github_change_formatter_bounds_oversized_commit_message():
    repo = {
        "repo_owner": "OpenAI",
        "repo_name": "openai-python",
    }
    rendered = format_check_result(
        {
            "repo": repo,
            "changes": [
                {
                    "type": "commit",
                    "message": "x" * (QQ_TEXT_SAFE_LIMIT * 2),
                    "author": "alice",
                }
            ],
        }
    )

    assert len(rendered) <= QQ_TEXT_SAFE_LIMIT
    assert "内容过长已截断" in rendered


async def test_stale_fetch_cannot_overwrite_newer_snapshot(db):
    repo_url = "https://github.com/OpenAI/openai-python"
    seed = GitHubTracker(db, FakeGitHubClient(_snapshot("base", stars=1)))
    repo = await seed.add_repository("user-1", repo_url)
    await seed.check("user-1", repo_url)

    fetch_started = asyncio.Event()
    release_old = asyncio.Event()

    class BlockingClient:
        async def fetch_snapshot(self, ref):
            del ref
            fetch_started.set()
            await release_old.wait()
            return _snapshot("old", stars=2)

    stale_tracker = GitHubTracker(db, BlockingClient())
    fresh_tracker = GitHubTracker(
        db,
        FakeGitHubClient(_snapshot("new", stars=3)),
    )

    stale_task = asyncio.create_task(
        stale_tracker.check("user-1", repo_url)
    )
    await fetch_started.wait()

    await fresh_tracker.check("user-1", repo_url)
    release_old.set()
    await stale_task

    latest = await db.fetch_latest_github_snapshot(repo["id"])
    assert latest is not None
    assert latest["latest_commit_sha"] == "new"
    assert latest["stars"] == 3
    assert await db.fetchone(
        "SELECT COUNT(*) AS count FROM github_snapshots WHERE repo_id = ?",
        (repo["id"],),
    ) == {"count": 2}


async def test_check_does_not_recreate_snapshot_after_repository_is_deleted(db):
    repo_url = "https://github.com/OpenAI/openai-python"
    seed = GitHubTracker(db, FakeGitHubClient(_snapshot("base")))
    repo = await seed.add_repository("user-1", repo_url)
    await seed.check("user-1", repo_url)

    fetch_started = asyncio.Event()
    release_fetch = asyncio.Event()

    class BlockingClient:
        async def fetch_snapshot(self, ref):
            del ref
            fetch_started.set()
            await release_fetch.wait()
            return _snapshot("later", stars=2)

    checking = GitHubTracker(db, BlockingClient())
    check_task = asyncio.create_task(
        checking.check("user-1", repo_url)
    )
    await fetch_started.wait()

    assert await seed.remove_repository("user-1", repo_url) is True
    release_fetch.set()
    await check_task

    assert await db.fetch_github_repository(
        "user-1",
        "OpenAI",
        "openai-python",
    ) is None
    assert await db.fetchone(
        "SELECT COUNT(*) AS count FROM github_snapshots WHERE repo_id = ?",
        (repo["id"],),
    ) == {"count": 0}
    assert await db.fetchone(
        "SELECT COUNT(*) AS count FROM outbound_messages",
        (),
    ) == {"count": 0}


async def test_watch_cannot_create_orphan_notification_after_repository_delete(tmp_path):
    reached_upsert = asyncio.Event()
    release_upsert = asyncio.Event()

    class BlockingWatchDatabase(Database):
        async def upsert_github_notification(
            self,
            repo_id: int,
            target_type: str,
            target_id: str,
            enabled: bool = True,
        ) -> bool:
            reached_upsert.set()
            await release_upsert.wait()
            return await super().upsert_github_notification(
                repo_id,
                target_type,
                target_id,
                enabled,
            )

    path = str(tmp_path / "github-watch-delete-race.db")
    watch_db = BlockingWatchDatabase(path)
    delete_db = Database(path)
    await watch_db.connect()
    await delete_db.connect()
    try:
        watch_tracker = GitHubTracker(
            watch_db,
            FakeGitHubClient(_snapshot("base")),
        )
        delete_tracker = GitHubTracker(
            delete_db,
            FakeGitHubClient(_snapshot("base")),
        )
        repo_url = "https://github.com/OpenAI/openai-python"
        await watch_tracker.add_repository("user-1", repo_url)

        watch_task = asyncio.create_task(
            watch_tracker.watch("user-1", repo_url, "user:999")
        )
        await reached_upsert.wait()

        assert await delete_tracker.remove_repository("user-1", repo_url) is True
        release_upsert.set()

        with pytest.raises(GitHubTrackerError, match="已被删除"):
            await watch_task

        assert await watch_db.fetchone(
            "SELECT COUNT(*) AS count FROM github_repositories",
            (),
        ) == {"count": 0}
        assert await watch_db.fetchone(
            "SELECT COUNT(*) AS count FROM github_notifications",
            (),
        ) == {"count": 0}
    finally:
        release_upsert.set()
        await watch_db.close()
        await delete_db.close()


async def test_tracker_watch_rejects_privileged_target_for_non_admin(db):
    tracker = GitHubTracker(
        db,
        FakeGitHubClient(_snapshot("first")),
        is_admin=lambda _user_id: False,
    )
    repo = await tracker.add_repository(
        "10001",
        "https://github.com/OpenAI/openai-python",
    )

    await tracker.watch("10001", repo["repo_url"], "user:10001")

    with pytest.raises(GitHubTrackerError, match="只能"):
        await tracker.watch("10001", repo["repo_url"], "group:123")

    with pytest.raises(GitHubTrackerError, match="只能"):
        await tracker.watch("10001", repo["repo_url"], "user:999")


async def test_revoked_admin_github_targets_are_disabled_before_delivery(db):
    admins = {"10001"}
    client = FakeGitHubClient(_snapshot("first"))
    dispatcher = FakeDispatcher()
    notifications = NotificationSettingsService(db)
    await notifications.set("10001", github_notify=True)
    tracker = GitHubTracker(
        db,
        client,
        dispatcher,
        notifications,
        is_admin=lambda user_id: user_id in admins,
    )
    repo = await tracker.add_repository(
        "10001",
        "https://github.com/OpenAI/openai-python",
    )
    await tracker.watch("10001", repo["repo_url"], "user:10001")
    await tracker.watch("10001", repo["repo_url"], "user:999")
    await tracker.watch("10001", repo["repo_url"], "group:123")
    await tracker.check("10001", repo["repo_url"])

    admins.clear()
    client.snapshot = _snapshot("second", stars=2)
    await tracker.run_scheduled_check({})

    outbound = await db.fetchall(
        "SELECT target_type, target_id, status FROM outbound_messages ORDER BY id",
        (),
    )
    assert outbound == [
        {"target_type": "user", "target_id": "10001", "status": "pending"}
    ]
    enabled_targets = await db.fetch_github_notifications(repo["id"])
    assert [
        (target["target_type"], target["target_id"])
        for target in enabled_targets
    ] == [("user", "10001")]
    all_targets = await db.fetchall(
        "SELECT target_type, target_id, enabled FROM github_notifications "
        "WHERE repo_id = ? ORDER BY target_type, target_id",
        (repo["id"],),
    )
    assert all_targets == [
        {"target_type": "group", "target_id": "123", "enabled": 0},
        {"target_type": "user", "target_id": "10001", "enabled": 1},
        {"target_type": "user", "target_id": "999", "enabled": 0},
    ]
    assert dispatcher.wake_calls == 1


async def test_github_notify_disable_committed_before_snapshot_enqueue_blocks_delivery(tmp_path):
    path = str(tmp_path / "github-notify-disable-race.db")
    settings_db = Database(path)
    check_db = Database(path)
    await settings_db.connect()
    await check_db.connect()
    try:
        tracker = GitHubTracker(
            settings_db,
            FakeGitHubClient(_snapshot("first")),
        )
        repo = await tracker.add_repository(
            "user-1",
            "https://github.com/OpenAI/openai-python",
        )
        previous_id = await settings_db.insert_github_snapshot(
            repo["id"],
            _snapshot("first").to_dict(),
        )
        await settings_db.upsert_github_notification(
            repo["id"],
            "user",
            "user-1",
            True,
        )
        await NotificationSettingsService(settings_db).set(
            "user-1",
            github_notify=True,
        )

        deliveries = [
            {
                "target_type": "user",
                "target_id": "user-1",
                "content": "change",
            }
        ]

        async with settings_db.operation():
            await settings_db.conn.execute("BEGIN IMMEDIATE")
            await settings_db.conn.execute(
                "UPDATE notification_settings SET github_notify = 0 "
                "WHERE user_id = ?",
                ("user-1",),
            )
            saving = asyncio.create_task(
                check_db.insert_github_snapshot_and_outbound(
                    repo["id"],
                    _snapshot("second", stars=2).to_dict(),
                    deliveries,
                    3,
                    previous_id,
                )
            )
            await asyncio.sleep(0.05)
            assert saving.done() is False
            await settings_db.conn.commit()

        snapshot_id, queued = await saving
        assert snapshot_id is not None
        assert queued == 0
        latest = await check_db.fetch_latest_github_snapshot(repo["id"])
        assert latest is not None
        assert latest["latest_commit_sha"] == "second"
        assert await check_db.fetchone(
            "SELECT COUNT(*) AS count FROM outbound_messages",
            (),
        ) == {"count": 0}
    finally:
        await settings_db.close()
        await check_db.close()


async def test_digest_clear_committed_before_enqueue_blocks_stale_targets(tmp_path):
    path = str(tmp_path / "github-digest-clear-race.db")
    settings_db = Database(path)
    digest_db = Database(path)
    await settings_db.connect()
    await digest_db.connect()
    try:
        await settings_db.replace_github_digest_targets(
            [
                {"target_type": "user", "target_id": "10001"},
                {"target_type": "group", "target_id": "20002"},
            ]
        )

        async with settings_db.operation():
            await settings_db.conn.execute("BEGIN IMMEDIATE")
            await settings_db.conn.execute("DELETE FROM github_digest_targets")
            enqueueing = asyncio.create_task(
                digest_db.enqueue_current_github_digest_targets(
                    "digest body",
                    3,
                )
            )
            await asyncio.sleep(0.05)
            assert enqueueing.done() is False
            await settings_db.conn.commit()

        assert await enqueueing == 0
        assert await digest_db.fetchone(
            "SELECT COUNT(*) AS count FROM outbound_messages",
            (),
        ) == {"count": 0}
    finally:
        await settings_db.close()
        await digest_db.close()


async def test_github_client_rejects_oversized_success_response():
    body = b"x" * (2 * 1024 * 1024 + 1)

    client = GitHubClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, content=body)
        )
    )
    try:
        with pytest.raises(GitHubAPIError, match="响应过大"):
            await client.get_repository("OpenAI", "openai-python")
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/" + ("a" * 101) + "/repo",
        "https://github.com/owner/" + ("b" * 101),
        "https://github.com/" + ("x" * 600) + "/repo",
    ],
)
def test_parse_repo_url_rejects_oversized_components(url):
    with pytest.raises(GitHubTrackerError):
        parse_repo_url(url)


def test_parse_snapshot_payload_bounds_untrusted_text_and_counts():
    huge = "x" * 10000
    snapshot = parse_snapshot_payload(
        {
            "stargazers_count": 10**100,
            "forks_count": -123,
            "subscribers_count": "not-a-number",
            "open_issues_count": 10**100,
        },
        {
            "sha": huge,
            "commit": {
                "message": huge,
                "author": {"name": huge, "date": huge},
            },
            "author": {"login": huge},
        },
        {"tag_name": huge},
        commits_count=10**100,
    )

    assert len(snapshot.latest_commit_sha) == 128
    assert len(snapshot.latest_commit_message) == 4000
    assert len(snapshot.latest_commit_author) == 256
    assert len(snapshot.latest_commit_time) == 128
    assert snapshot.latest_release is not None
    assert len(snapshot.latest_release) == 256
    assert snapshot.stars == (1 << 63) - 1
    assert snapshot.forks == 0
    assert snapshot.watchers == 0
    assert snapshot.commits_count == (1 << 63) - 1
    assert snapshot.open_issues_count == (1 << 63) - 1


async def test_snapshot_history_limit_prunes_old_rows_for_direct_insert(db, monkeypatch):
    monkeypatch.setattr(
        github_repository_module,
        "_GITHUB_SNAPSHOT_HISTORY_LIMIT",
        3,
    )
    repo_id = await db.insert_github_repository(
        "user-1",
        "OpenAI",
        "openai-python",
        "https://github.com/OpenAI/openai-python",
    )
    assert repo_id is not None

    for i in range(5):
        await db.insert_github_snapshot(
            repo_id,
            _snapshot(f"sha-{i}", stars=i).to_dict(),
        )

    rows = await db.fetchall(
        "SELECT latest_commit_sha FROM github_snapshots "
        "WHERE repo_id = ? ORDER BY id",
        (repo_id,),
    )
    assert rows == [
        {"latest_commit_sha": "sha-2"},
        {"latest_commit_sha": "sha-3"},
        {"latest_commit_sha": "sha-4"},
    ]


async def test_tracker_snapshot_history_limit_prunes_old_rows(db, monkeypatch):
    monkeypatch.setattr(
        github_repository_module,
        "_GITHUB_SNAPSHOT_HISTORY_LIMIT",
        3,
    )
    client = FakeGitHubClient(_snapshot("sha-0", stars=0))
    tracker = GitHubTracker(db, client)
    repo = await tracker.add_repository(
        "user-1",
        "https://github.com/OpenAI/openai-python",
    )

    for i in range(5):
        client.snapshot = _snapshot(f"sha-{i}", stars=i)
        await tracker.check("user-1", repo["repo_url"])

    rows = await db.fetchall(
        "SELECT latest_commit_sha FROM github_snapshots "
        "WHERE repo_id = ? ORDER BY id",
        (repo["id"],),
    )
    assert rows == [
        {"latest_commit_sha": "sha-2"},
        {"latest_commit_sha": "sha-3"},
        {"latest_commit_sha": "sha-4"},
    ]


async def test_digest_configuration_rolls_back_targets_and_schedule_on_live_failure(
    db,
    monkeypatch,
):
    from rumi_helper.services.scheduler import SchedulerService

    tracker = GitHubTracker(db, FakeGitHubClient(_snapshot("first")))
    scheduler = SchedulerService(db, FakeDispatcher(), timezone_name="UTC")
    scheduler.register_handler("github_digest", tracker.run_scheduled_digest)
    old_targets = [{"target_type": "user", "target_id": "111"}]
    await db.replace_github_digest_targets(old_targets)
    await scheduler.sync_system_task("github_digest", "0 6 * * *")

    await scheduler.start()
    try:
        task = await db.fetch_scheduled_task_by_owner_type(
            "__system__",
            "github_digest",
        )
        assert task is not None
        job_id = scheduler._job_id(task["id"])
        old_trigger = str(scheduler._scheduler.get_job(job_id).trigger)
        original_schedule = scheduler._schedule
        failed = False

        def fail_new_schedule_once(task_row):
            nonlocal failed
            if (
                not failed
                and task_row["task_type"] == "github_digest"
                and task_row["cron_expression"] == "5 * * * *"
            ):
                failed = True
                raise RuntimeError("injected live scheduler failure")
            return original_schedule(task_row)

        monkeypatch.setattr(scheduler, "_schedule", fail_new_schedule_once)

        with pytest.raises(
            GitHubTrackerError,
            match="定时汇总配置更新失败",
        ):
            await tracker.configure_digest_targets(
                [{"target_type": "group", "target_id": "222"}],
                scheduler,
                "5 * * * *",
            )

        assert await db.fetch_github_digest_targets() == old_targets
        restored = await db.fetch_scheduled_task_by_owner_type(
            "__system__",
            "github_digest",
        )
        assert restored["enabled"] == 1
        assert restored["cron_expression"] == "0 6 * * *"
        restored_job = scheduler._scheduler.get_job(job_id)
        assert restored_job is not None
        assert str(restored_job.trigger) == old_trigger
    finally:
        await scheduler.stop()


async def test_digest_configuration_cancellation_restores_previous_targets(db):
    tracker = GitHubTracker(db, FakeGitHubClient(_snapshot("first")))
    old_targets = [{"target_type": "user", "target_id": "111"}]
    await db.replace_github_digest_targets(old_targets)

    class CancellingScheduler:
        def __init__(self):
            self.calls: list[tuple[str, str]] = []

        async def sync_system_task(self, task_type: str, cron_expression: str):
            self.calls.append((task_type, cron_expression))
            if len(self.calls) == 1:
                raise asyncio.CancelledError

    scheduler = CancellingScheduler()

    with pytest.raises(asyncio.CancelledError):
        await tracker.configure_digest_targets(
            [{"target_type": "group", "target_id": "222"}],
            scheduler,
            "5 * * * *",
        )

    assert await db.fetch_github_digest_targets() == old_targets
    assert scheduler.calls == [
        ("github_digest", "5 * * * *"),
        ("github_digest", ""),
    ]


async def test_repository_identity_is_case_insensitive_per_owner(db):
    tracker = GitHubTracker(db, FakeGitHubClient(_snapshot("first")))

    first = await tracker.add_repository(
        "user-1",
        "https://github.com/OpenAI/openai-python",
    )
    second = await tracker.add_repository(
        "user-1",
        "https://github.com/openai/OPENAI-PYTHON",
    )

    assert second["id"] == first["id"]
    repos = await tracker.list_repositories("user-1")
    assert len(repos) == 1
    assert repos[0]["id"] == first["id"]

    assert await tracker.remove_repository(
        "user-1",
        "https://github.com/OPENAI/OpenAI-Python",
    )
    assert await tracker.list_repositories("user-1") == []
