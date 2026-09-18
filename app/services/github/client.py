"""GitHub REST API 客户端。"""

import re
from typing import Any

import httpx

from app.services.github.models import GitHubRepoRef, GitHubSnapshot

_LINK_LAST_PAGE = re.compile(r"[?&]page=(\d+)[^>]*>;\s*rel=\"last\"")


class GitHubAPIError(Exception):
    """GitHub API 请求失败。"""


class GitHubRateLimitError(GitHubAPIError):
    """GitHub API 达到速率限制。"""


def parse_snapshot_payload(
    repository: dict[str, Any],
    commit: dict[str, Any],
    release: dict[str, Any] | None,
    *,
    commits_count: int,
) -> GitHubSnapshot:
    commit_data = commit.get("commit") or {}
    commit_author = commit.get("author") or {}
    embedded_author = commit_data.get("author") or {}
    author = commit_author.get("login") or embedded_author.get("name") or "unknown"
    return GitHubSnapshot(
        stars=int(repository.get("stargazers_count", 0)),
        forks=int(repository.get("forks_count", 0)),
        watchers=int(repository.get("subscribers_count", repository.get("watchers_count", 0))),
        commits_count=int(commits_count),
        latest_commit_sha=str(commit.get("sha") or ""),
        latest_commit_message=str(commit_data.get("message") or ""),
        latest_commit_author=str(author),
        latest_commit_time=str(embedded_author.get("date") or ""),
        latest_release=str(release.get("tag_name")) if release and release.get("tag_name") else None,
        open_issues_count=int(repository.get("open_issues_count", 0)),
    )


class GitHubClient:
    def __init__(
        self,
        token: str = "",
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
        api_base_url: str = "https://api.github.com",
    ):
        self._client = httpx.AsyncClient(
            base_url=api_base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "qq-llm-bot/0.1",
                **({"Authorization": f"Bearer {token}"} if token else {}),
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_repository(self, owner: str, name: str) -> dict:
        return await self._get_json(f"/repos/{owner}/{name}")

    async def check_health(self) -> None:
        await self._get("/rate_limit")

    async def fetch_snapshot(self, ref: GitHubRepoRef) -> GitHubSnapshot:
        repository = await self.get_repository(ref.owner, ref.name)
        commits_response = await self._get(f"/repos/{ref.owner}/{ref.name}/commits", params={"per_page": 1})
        commits = self._decode_json(commits_response)
        if not isinstance(commits, list):
            raise GitHubAPIError("GitHub commits 响应格式无效")
        commit = commits[0] if commits else {}
        commits_count = self._commits_count(commits_response, len(commits))
        release: dict | None = None
        try:
            release = await self._get_json(f"/repos/{ref.owner}/{ref.name}/releases/latest")
        except GitHubAPIError as e:
            if not str(e).startswith("GitHub API HTTP 404"):
                raise
        return parse_snapshot_payload(repository, commit, release, commits_count=commits_count)

    async def _get_json(self, path: str) -> dict:
        response = await self._get(path)
        data = self._decode_json(response)
        if not isinstance(data, dict):
            raise GitHubAPIError("GitHub API 响应格式无效")
        return data

    @staticmethod
    def _decode_json(response: httpx.Response) -> object:
        try:
            return response.json()
        except ValueError as e:
            raise GitHubAPIError("GitHub API 响应格式无效") from e

    async def _get(self, path: str, *, params: dict | None = None) -> httpx.Response:
        try:
            response = await self._client.get(path, params=params)
        except httpx.TimeoutException as e:
            raise GitHubAPIError("GitHub API 请求超时") from e
        except httpx.HTTPError as e:
            raise GitHubAPIError(f"GitHub API 请求失败：{type(e).__name__}") from e
        if response.status_code >= 400:
            if response.status_code == 403 and response.headers.get("X-RateLimit-Remaining") == "0":
                raise GitHubRateLimitError("GitHub API 已达到速率限制")
            raise GitHubAPIError(f"GitHub API HTTP {response.status_code}: {response.text[:200]}")
        return response

    @staticmethod
    def _commits_count(response: httpx.Response, fallback: int) -> int:
        link = response.headers.get("Link", "")
        match = _LINK_LAST_PAGE.search(link)
        return int(match.group(1)) if match else fallback
