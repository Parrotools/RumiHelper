"""Database methods for GitHubRepository."""

from rumi_helper.database.repositories.common import _now, _outbound_content


class GitHubRepository:
    async def insert_github_repository(
        self,
        owner_id: str,
        repo_owner: str,
        repo_name: str,
        repo_url: str,
        max_per_owner: int = 50,
    ) -> int | None:
        async with self.operation():
            try:
                await self.conn.execute("BEGIN IMMEDIATE")
                row = await self.fetchone(
                    "SELECT id FROM github_repositories "
                    "WHERE owner_id = ? AND repo_owner = ? AND repo_name = ?",
                    (owner_id, repo_owner, repo_name),
                )
                if row is not None:
                    await self.conn.execute(
                        "UPDATE github_repositories SET repo_url = ? WHERE id = ?",
                        (repo_url, int(row["id"])),
                    )
                    await self.conn.commit()
                    return int(row["id"])

                count = await self.fetchone(
                    "SELECT COUNT(*) AS count FROM github_repositories WHERE owner_id = ?",
                    (owner_id,),
                )
                if int(count["count"]) >= max(1, int(max_per_owner)):
                    await self.conn.commit()
                    return None

                async with self.conn.execute(
                    "INSERT INTO github_repositories "
                    "(owner_id, repo_owner, repo_name, repo_url) VALUES (?, ?, ?, ?)",
                    (owner_id, repo_owner, repo_name, repo_url),
                ) as cursor:
                    repo_id = int(cursor.lastrowid)
                await self.conn.commit()
                return repo_id
            except Exception:
                await self.conn.rollback()
                raise

    async def fetch_github_repository(
        self, owner_id: str, repo_owner: str, repo_name: str
    ) -> dict | None:
        return await self.fetchone(
            "SELECT * FROM github_repositories WHERE owner_id = ? AND repo_owner = ? AND repo_name = ?",
            (owner_id, repo_owner, repo_name),
        )

    async def fetch_github_repositories(self, owner_id: str | None = None) -> list[dict]:
        if owner_id is None:
            return await self.fetchall("SELECT * FROM github_repositories ORDER BY id")
        return await self.fetchall(
            "SELECT * FROM github_repositories WHERE owner_id = ? ORDER BY id", (owner_id,)
        )

    async def fetch_github_digest_targets(self) -> list[dict]:
        return await self.fetchall(
            "SELECT target_type, target_id FROM github_digest_targets WHERE enabled = 1 "
            "ORDER BY target_type, target_id"
        )

    async def replace_github_digest_targets(self, targets: list[dict]) -> None:
        unique_targets = dict.fromkeys(
            (str(target["target_type"]), str(target["target_id"])) for target in targets
        )
        async with self.operation():
            try:
                await self.conn.execute("DELETE FROM github_digest_targets")
                await self.conn.executemany(
                    "INSERT INTO github_digest_targets (target_type, target_id, enabled) VALUES (?, ?, 1)",
                    list(unique_targets),
                )
                await self.conn.commit()
            except Exception:
                await self.conn.rollback()
                raise

    async def delete_github_repository(self, owner_id: str, repo_owner: str, repo_name: str) -> bool:
        where = (owner_id, repo_owner, repo_name)
        async with self.operation():
            try:
                await self.conn.execute(
                    "DELETE FROM github_snapshots WHERE repo_id IN "
                    "(SELECT id FROM github_repositories WHERE owner_id = ? AND repo_owner = ? AND repo_name = ?)",
                    where,
                )
                await self.conn.execute(
                    "DELETE FROM github_notifications WHERE repo_id IN "
                    "(SELECT id FROM github_repositories WHERE owner_id = ? AND repo_owner = ? AND repo_name = ?)",
                    where,
                )
                async with self.conn.execute(
                    "DELETE FROM github_repositories WHERE owner_id = ? AND repo_owner = ? AND repo_name = ?",
                    where,
                ) as cursor:
                    deleted = cursor.rowcount > 0
                await self.conn.commit()
                return deleted
            except Exception:
                await self.conn.rollback()
                raise

    async def _insert_github_snapshot_unlocked(self, repo_id: int, snapshot: dict) -> int:
        cursor = await self.conn.execute(
            "INSERT INTO github_snapshots "
            "(repo_id, stars, forks, watchers, commits_count, latest_commit_sha, latest_commit_message, "
            "latest_commit_author, latest_commit_time, latest_release, open_issues_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                repo_id,
                snapshot["stars"],
                snapshot["forks"],
                snapshot["watchers"],
                snapshot["commits_count"],
                snapshot["latest_commit_sha"],
                snapshot["latest_commit_message"],
                snapshot["latest_commit_author"],
                snapshot["latest_commit_time"],
                snapshot["latest_release"],
                snapshot["open_issues_count"],
            ),
        )
        return int(cursor.lastrowid)

    async def insert_github_snapshot(self, repo_id: int, snapshot: dict) -> int:
        async with self.operation():
            snapshot_id = await self._insert_github_snapshot_unlocked(repo_id, snapshot)
            await self.conn.commit()
            return snapshot_id

    async def insert_github_snapshot_and_outbound(
        self,
        repo_id: int,
        snapshot: dict,
        deliveries: list[dict],
        max_attempts: int,
        expected_previous_id: int,
    ) -> int | None:
        """CAS the latest snapshot, then queue every derived notification atomically."""
        async with self.operation():
            try:
                async with self.conn.execute(
                    "INSERT INTO github_snapshots "
                    "(repo_id, stars, forks, watchers, commits_count, latest_commit_sha, latest_commit_message, "
                    "latest_commit_author, latest_commit_time, latest_release, open_issues_count) "
                    "SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ? "
                    "WHERE COALESCE(("
                    "  SELECT MAX(id) FROM github_snapshots WHERE repo_id = ?"
                    "), 0) = ? "
                    "AND EXISTS (SELECT 1 FROM github_repositories WHERE id = ?)",
                    (
                        repo_id,
                        snapshot["stars"],
                        snapshot["forks"],
                        snapshot["watchers"],
                        snapshot["commits_count"],
                        snapshot["latest_commit_sha"],
                        snapshot["latest_commit_message"],
                        snapshot["latest_commit_author"],
                        snapshot["latest_commit_time"],
                        snapshot["latest_release"],
                        snapshot["open_issues_count"],
                        repo_id,
                        int(expected_previous_id),
                        repo_id,
                    ),
                ) as cursor:
                    if cursor.rowcount != 1:
                        await self.conn.commit()
                        return None
                    snapshot_id = int(cursor.lastrowid)

                if deliveries:
                    next_attempt_at = _now()
                    await self.conn.executemany(
                        "INSERT INTO outbound_messages "
                        "(target_type, target_id, content, max_attempts, next_attempt_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        [
                            (
                                str(delivery["target_type"]),
                                str(delivery["target_id"]),
                                _outbound_content(delivery["content"]),
                                max(1, int(max_attempts)),
                                next_attempt_at,
                            )
                            for delivery in deliveries
                        ],
                    )
                await self.conn.commit()
                return snapshot_id
            except Exception:
                await self.conn.rollback()
                raise

    async def fetch_latest_github_snapshot(self, repo_id: int) -> dict | None:
        return await self.fetchone(
            "SELECT * FROM github_snapshots WHERE repo_id = ? ORDER BY id DESC LIMIT 1", (repo_id,)
        )

    async def upsert_github_notification(
        self, repo_id: int, target_type: str, target_id: str, enabled: bool = True
    ) -> None:
        await self.execute(
            "INSERT INTO github_notifications (repo_id, target_type, target_id, enabled) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(repo_id, target_type, target_id) DO UPDATE SET enabled = excluded.enabled",
            (repo_id, target_type, target_id, int(enabled)),
        )

    async def fetch_github_notifications(self, repo_id: int) -> list[dict]:
        return await self.fetchall(
            "SELECT * FROM github_notifications WHERE repo_id = ? AND enabled = 1 ORDER BY target_type, target_id",
            (repo_id,),
        )

    async def fetch_github_snapshots_between(self, repo_id: int, period_start: str, period_end: str) -> list[dict]:
        return await self.fetchall(
            "SELECT * FROM github_snapshots WHERE repo_id = ? AND datetime(captured_at) >= datetime(?) "
            "AND datetime(captured_at) < datetime(?) ORDER BY id",
            (repo_id, period_start, period_end),
        )

    async def fetch_latest_github_snapshot_before(self, repo_id: int, period_start: str) -> dict | None:
        return await self.fetchone(
            "SELECT * FROM github_snapshots WHERE repo_id = ? AND datetime(captured_at) < datetime(?) "
            "ORDER BY id DESC LIMIT 1",
            (repo_id, period_start),
        )
