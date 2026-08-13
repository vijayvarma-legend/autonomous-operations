from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from app.agents.investigators.git_diff_investigator import investigate
from app.models import AuditLogEntry, Incident, IncidentSource

DETECTED_AT = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)


@dataclass
class FakeGitAuthor:
    name: str
    date: datetime


@dataclass
class FakeGitCommit:
    author: FakeGitAuthor | None
    message: str


@dataclass
class FakeFile:
    filename: str
    status: str
    additions: int
    deletions: int


@dataclass
class FakeCommit:
    sha: str
    commit: FakeGitCommit
    html_url: str
    files: list[FakeFile]


class FakeRepo:
    def __init__(self, commits, default_branch="main"):
        self._commits = commits
        self.default_branch = default_branch
        self.requested_sha = None
        self.requested_since = None
        self.requested_until = None

    def get_commits(self, sha=None, since=None, until=None):
        self.requested_sha = sha
        self.requested_since = since
        self.requested_until = until
        return self._commits


class FakeGithub:
    def __init__(self, repo):
        self._repo = repo

    def get_repo(self, name):
        return self._repo


def _incident(**overrides):
    defaults = dict(
        source=IncidentSource.GITHUB_ACTIONS,
        external_id="run-1",
        title="CI failure: build on main",
        detected_at=DETECTED_AT,
        raw_payload={"head_branch": "main"},
    )
    return Incident(**{**defaults, **overrides})


def _commit(sha, message, files=None):
    return FakeCommit(
        sha=sha,
        commit=FakeGitCommit(author=FakeGitAuthor(name="alice", date=DETECTED_AT), message=message),
        html_url=f"https://github.com/x/y/commit/{sha}",
        files=files or [FakeFile(filename="app.py", status="modified", additions=3, deletions=1)],
    )


def test_investigate_collects_commits_and_records_audit_entry(db_session, monkeypatch):
    monkeypatch.setattr("app.config.settings.github_token", "tok")
    monkeypatch.setattr("app.config.settings.github_repo", "owner/repo")

    incident = _incident()
    db_session.add(incident)
    db_session.flush()

    commits = [_commit("abc123", "fix: handle null response")]
    repo = FakeRepo(commits)
    client = FakeGithub(repo)

    result = investigate(db_session, incident, client=client)

    assert result[0]["sha"] == "abc123"
    assert result[0]["author"] == "alice"
    assert result[0]["message"] == "fix: handle null response"
    assert result[0]["files"][0]["filename"] == "app.py"
    assert repo.requested_sha == "main"  # from incident.raw_payload["head_branch"]
    assert repo.requested_until == DETECTED_AT

    entry = (
        db_session.query(AuditLogEntry)
        .filter(
            AuditLogEntry.incident_id == incident.id,
            AuditLogEntry.actor == "git_diff_investigator",
        )
        .one()
    )
    assert entry.action == "evidence_gathered"
    assert entry.detail["branch"] == "main"
    assert len(entry.detail["commits"]) == 1


def test_investigate_falls_back_to_default_branch(db_session, monkeypatch):
    monkeypatch.setattr("app.config.settings.github_token", "tok")
    monkeypatch.setattr("app.config.settings.github_repo", "owner/repo")

    incident = _incident(external_id="run-2", raw_payload={})  # no head_branch
    db_session.add(incident)
    db_session.flush()

    repo = FakeRepo([], default_branch="develop")
    client = FakeGithub(repo)

    investigate(db_session, incident, client=client)

    assert repo.requested_sha == "develop"


def test_investigate_requires_github_token(db_session, monkeypatch):
    monkeypatch.setattr("app.config.settings.github_token", "")
    monkeypatch.setattr("app.config.settings.github_repo", "owner/repo")

    incident = _incident(external_id="run-3")
    db_session.add(incident)
    db_session.flush()

    with pytest.raises(RuntimeError, match="GITHUB_TOKEN"):
        investigate(db_session, incident)


def test_investigate_requires_github_repo(db_session, monkeypatch):
    monkeypatch.setattr("app.config.settings.github_token", "tok")
    monkeypatch.setattr("app.config.settings.github_repo", "")

    incident = _incident(external_id="run-4")
    db_session.add(incident)
    db_session.flush()

    with pytest.raises(RuntimeError, match="GITHUB_REPO"):
        investigate(db_session, incident)
