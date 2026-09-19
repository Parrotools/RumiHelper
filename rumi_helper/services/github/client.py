"""GitHub REST API 客户端。"""

import re
from typing import Any

import httpx

from rumi_helper.services.github.models import GitHubRepoRef, GitHubSnapshot
from rumi_helper.services.http_utils import (
    ResponseBodyTooLargeError,
    read_limited_response_body,
)

_LINK_LAST_PAGE = re.compile(r"[?&]page=(\d+)[^>]*>;\s*rel=\"last\"")
_MAX_GITHUB_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_SHA_CHARS = 128
_MAX_COMMIT_MESSAGE_CHARS = 4000
_MAX_AUTHOR_CHARS = 256
_MAX_COMMIT_TIME_CHARS = 128
_MAX_RELEASE_CHARS = 256
_MAX_SQLITE_INTEGER = (1 << 63) - 1


def _bounded_text(value: object, limit: int) -> str:
    return str(value or "")[: max(0, int(limit))]


def _bounded_count(value: object) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, min(number, _MAX_SQLITE_INTEGER))


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
    release_tag = release.get("tag_name") if release else None
    return GitHubSnapshot(
        stars=_bounded_count(repository.get("stargazers_count", 0)),
        forks=_bounded_count(repository.get("forks_count", 0)),
        watchers=_bounded_count(
            repository.get("subscribers_count", repository.get("watchers_count", 0))
        ),
        commits_count=_bounded_count(commits_count),
        latest_commit_sha=_bounded_text(commit.get("sha"), _MAX_SHA_CHARS),
        latest_commit_message=_bounded_text(
            commit_data.get("message"),
            _MAX_COMMIT_MESSAGE_CHARS,
        ),
        latest_commit_author=_bounded_text(author, _MAX_AUTHOR_CHARS),
        latest_commit_time=_bounded_text(
            embedded_author.get("date"),
            _MAX_COMMIT_TIME_CHARS,
        ),
        latest_release=(
            _bounded_text(release_tag, _MAX_RELEASE_CHARS)
            if release_tag
            else None
        ),
        open_issues_count=_bounded_count(repository.get("open_issues_count", 0)),
    )


class GitHubClient:
    def __init__(
        self,
        token: str | None = None,
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
            async with self._client.stream("GET", path, params=params) as response:
                if response.status_code >= 400:
                    if (
                        response.status_code == 403
                        and response.headers.get("X-RateLimit-Remaining") == "0"
                    ):
                        raise GitHubRateLimitError("GitHub API 已达到速率限制")
                    # Do not read or propagate arbitrary remote error bodies.
                    raise GitHubAPIError(f"GitHub API HTTP {response.status_code}")
                try:
                    body = await read_limited_response_body(
                        response,
                        _MAX_GITHUB_RESPONSE_BYTES,
                    )
                except ResponseBodyTooLargeError as e:
                    raise GitHubAPIError("GitHub API 响应过大") from e
                return httpx.Response(
                    response.status_code,
                    headers=response.headers,
                    content=body,
                    request=response.request,
                )
        except httpx.TimeoutException as e:
            raise GitHubAPIError("GitHub API 请求超时") from e
        except httpx.HTTPError as e:
            raise GitHubAPIError(f"GitHub API 请求失败：{type(e).__name__}") from e

    @staticmethod
    def _commits_count(response: httpx.Response, fallback: int) -> int:
        link = response.headers.get("Link", "")
        match = _LINK_LAST_PAGE.search(link)
        return int(match.group(1)) if match else fallback
