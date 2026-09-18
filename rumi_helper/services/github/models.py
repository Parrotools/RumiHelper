"""GitHub 领域模型。"""

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class GitHubRepoRef:
    owner: str
    name: str
    url: str


@dataclass(frozen=True)
class GitHubSnapshot:
    stars: int
    forks: int
    watchers: int
    commits_count: int
    latest_commit_sha: str
    latest_commit_message: str
    latest_commit_author: str
    latest_commit_time: str
    latest_release: str | None
    open_issues_count: int

    def to_dict(self) -> dict:
        return asdict(self)
