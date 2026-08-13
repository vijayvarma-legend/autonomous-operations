from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from app.models import IncidentSource
from app.sources.github_actions import fetch_failed_workflow_runs


@dataclass
class FakeActor:
    login: str


@dataclass
class FakeRun:
    id: int
    name: str
    head_branch: str
    head_sha: str
    html_url: str
    run_attempt: int
    created_at: datetime
    actor: FakeActor | None


class FakeRepo:
    def __init__(self, runs, default_branch="main"):
        self._runs = runs
        self.default_branch = default_branch
        self.requested_branch = None
        self.requested_status = None

    def get_workflow_runs(self, branch=None, status=None):
        self.requested_branch = branch
        self.requested_status = status
        return self._runs


class FakeGithub:
    def __init__(self, repo):
        self._repo = repo
        self.requested_repo_name = None

    def get_repo(self, name):
        self.requested_repo_name = name
        return self._repo


def _run(run_id, created_at):
    return FakeRun(
        id=run_id,
        name="build",
        head_branch="main",
        head_sha="abc123",
        html_url=f"https://github.com/x/y/actions/runs/{run_id}",
        run_attempt=1,
        created_at=created_at,
        actor=FakeActor(login="alice"),
    )


def test_maps_runs_and_stops_at_cutoff(monkeypatch):
    monkeypatch.setattr("app.sources.github_actions.settings.github_token", "tok")
    monkeypatch.setattr("app.sources.github_actions.settings.github_repo", "owner/repo")

    runs = [
        _run(3, datetime(2026, 8, 10, tzinfo=timezone.utc)),
        _run(2, datetime(2026, 8, 5, tzinfo=timezone.utc)),
        _run(1, datetime(2026, 7, 1, tzinfo=timezone.utc)),  # before cutoff, must be excluded
    ]
    repo = FakeRepo(runs)
    client = FakeGithub(repo)

    since = datetime(2026, 8, 1, tzinfo=timezone.utc)
    result = fetch_failed_workflow_runs(since, client=client)

    assert [f.external_id for f in result] == ["3", "2"]
    assert result[0].source == IncidentSource.GITHUB_ACTIONS
    assert result[0].title == "CI failure: build on main"
    assert result[0].raw_payload["run_id"] == 3
    assert repo.requested_branch == "main"  # fell back to repo.default_branch
    assert repo.requested_status == "failure"


def test_uses_explicit_branch_over_default(monkeypatch):
    monkeypatch.setattr("app.sources.github_actions.settings.github_token", "tok")
    monkeypatch.setattr("app.sources.github_actions.settings.github_repo", "owner/repo")
    repo = FakeRepo([])
    client = FakeGithub(repo)

    fetch_failed_workflow_runs(
        datetime(2026, 1, 1, tzinfo=timezone.utc), branch="develop", client=client
    )

    assert repo.requested_branch == "develop"


def test_requires_github_token(monkeypatch):
    monkeypatch.setattr("app.sources.github_actions.settings.github_token", "")
    monkeypatch.setattr("app.sources.github_actions.settings.github_repo", "owner/repo")

    with pytest.raises(RuntimeError, match="GITHUB_TOKEN"):
        fetch_failed_workflow_runs(datetime.now(timezone.utc))


def test_requires_github_repo(monkeypatch):
    monkeypatch.setattr("app.sources.github_actions.settings.github_token", "tok")
    monkeypatch.setattr("app.sources.github_actions.settings.github_repo", "")

    with pytest.raises(RuntimeError, match="GITHUB_REPO"):
        fetch_failed_workflow_runs(datetime.now(timezone.utc))
