from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from app.agents.recovery import recover
from app.models import AuditLogEntry, Incident, IncidentSource, IncidentState

DETECTED_AT = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)


@dataclass
class FakeBranchCommit:
    sha: str


@dataclass
class FakeBranch:
    commit: FakeBranchCommit


@dataclass
class FakeContent:
    sha: str
    decoded_content: bytes


@dataclass
class FakeParentCommit:
    sha: str


@dataclass
class FakeCommit:
    parents: list[FakeParentCommit]


@dataclass
class FakePR:
    number: int
    html_url: str


class FakeRepo:
    def __init__(self, default_branch="main", file_contents=None):
        self.default_branch = default_branch
        self._file_contents = file_contents or {}  # {(path, ref): bytes}
        self.created_refs = []
        self.updated_files = []  # list of (path, message, content, sha, branch)
        self.created_pr = None

    def get_branch(self, name):
        return FakeBranch(commit=FakeBranchCommit(sha=f"{name}-sha"))

    def create_git_ref(self, ref, sha):
        self.created_refs.append((ref, sha))

    def get_contents(self, path, ref=None):
        content = self._file_contents[(path, ref)]
        return FakeContent(sha=f"{path}-{ref}-sha", decoded_content=content)

    def get_commit(self, sha):
        return FakeCommit(parents=[FakeParentCommit(sha=f"{sha}-parent")])

    def update_file(self, path, message, content, sha, branch=None):
        self.updated_files.append((path, message, content, sha, branch))

    def create_pull(self, title, body, head, base):
        self.created_pr = FakePR(number=42, html_url="https://github.com/x/y/pull/42")
        return self.created_pr


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


def _add_decision(db_session, incident, strategy, root_cause="bad commit", reasoning="because"):
    db_session.add(
        AuditLogEntry(
            incident_id=incident.id,
            actor="decision",
            action="diagnosis_completed",
            detail={
                "root_cause": root_cause,
                "confidence": 0.9,
                "recovery_strategy": strategy,
                "reasoning": reasoning,
            },
        )
    )
    db_session.flush()


def _add_git_evidence(db_session, incident, sha="abc123", files=None):
    db_session.add(
        AuditLogEntry(
            incident_id=incident.id,
            actor="git_diff_investigator",
            action="evidence_gathered",
            detail={"commits": [{"sha": sha, "files": files or [{"filename": "app.py"}]}]},
        )
    )
    db_session.flush()


def _set_github_config(monkeypatch, token="tok", repo="owner/repo"):
    monkeypatch.setattr("app.agents.recovery.settings.github_token", token)
    monkeypatch.setattr("app.agents.recovery.settings.github_repo", repo)


def test_recover_rollback_reverts_file_to_parent_content(db_session, monkeypatch):
    _set_github_config(monkeypatch)
    incident = _incident()
    db_session.add(incident)
    db_session.flush()
    _add_decision(db_session, incident, "rollback")
    _add_git_evidence(db_session, incident, sha="abc123")

    repo = FakeRepo(
        file_contents={
            ("app.py", "auto-fix/" + str(incident.id) + "-1"): b"broken content",
            ("app.py", "abc123-parent"): b"good content",
        }
    )
    client = FakeGithub(repo)

    result = recover(db_session, incident, client=client)

    assert result["status"] == "proposed"
    assert repo.updated_files[0][2] == "good content"
    assert repo.created_pr.number == 42
    assert incident.state == IncidentState.VERIFYING

    entry = (
        db_session.query(AuditLogEntry)
        .filter(AuditLogEntry.incident_id == incident.id, AuditLogEntry.actor == "recovery")
        .one()
    )
    assert entry.action == "fix_proposed"
    assert entry.detail["pr_number"] == 42
    assert entry.detail["files_changed"] == ["app.py"]


def test_recover_patch_uses_llm_drafted_content(db_session, monkeypatch):
    _set_github_config(monkeypatch)
    incident = _incident(external_id="run-2")
    db_session.add(incident)
    db_session.flush()
    _add_decision(db_session, incident, "patch")
    _add_git_evidence(db_session, incident)

    branch = "auto-fix/" + str(incident.id) + "-1"
    repo = FakeRepo(file_contents={("app.py", branch): b"def f(): pass"})
    client = FakeGithub(repo)

    monkeypatch.setattr(
        "app.agents.recovery.complete",
        lambda system, user: "```python\ndef f(): return 1\n```",
    )

    recover(db_session, incident, client=client)

    assert repo.updated_files[0][2] == "def f(): return 1"  # code fence stripped


def test_recover_restart_strategy_is_skipped(db_session, monkeypatch):
    _set_github_config(monkeypatch)
    incident = _incident(external_id="run-3")
    db_session.add(incident)
    db_session.flush()
    _add_decision(db_session, incident, "restart")

    result = recover(db_session, incident, client=object())  # never touched

    assert result["status"] == "skipped"
    assert incident.state == IncidentState.NEEDS_HUMAN_TRIAGE


def test_recover_enforces_max_recovery_attempts(db_session, monkeypatch):
    _set_github_config(monkeypatch)
    monkeypatch.setattr("app.agents.recovery.settings.max_recovery_attempts", 2)
    incident = _incident(external_id="run-4")
    db_session.add(incident)
    db_session.flush()
    _add_decision(db_session, incident, "patch")
    for _ in range(2):
        db_session.add(
            AuditLogEntry(
                incident_id=incident.id,
                actor="recovery",
                action="fix_proposed",
                detail={"strategy": "patch"},
            )
        )
    db_session.flush()

    result = recover(db_session, incident, client=object())  # never touched

    assert result["status"] == "capped"
    assert incident.state == IncidentState.ESCALATED


def test_recover_raises_without_decision(db_session, monkeypatch):
    _set_github_config(monkeypatch)
    incident = _incident(external_id="run-5")
    db_session.add(incident)
    db_session.flush()

    with pytest.raises(ValueError, match="No decision found"):
        recover(db_session, incident)


def test_recover_raises_without_git_evidence(db_session, monkeypatch):
    _set_github_config(monkeypatch)
    incident = _incident(external_id="run-6")
    db_session.add(incident)
    db_session.flush()
    _add_decision(db_session, incident, "patch")

    with pytest.raises(ValueError, match="No git diff evidence"):
        recover(db_session, incident)


def test_recover_requires_github_token(db_session, monkeypatch):
    _set_github_config(monkeypatch, token="")
    incident = _incident(external_id="run-7")
    db_session.add(incident)
    db_session.flush()

    with pytest.raises(RuntimeError, match="GITHUB_TOKEN"):
        recover(db_session, incident)


def test_recover_requires_github_repo(db_session, monkeypatch):
    _set_github_config(monkeypatch, repo="")
    incident = _incident(external_id="run-8")
    db_session.add(incident)
    db_session.flush()

    with pytest.raises(RuntimeError, match="GITHUB_REPO"):
        recover(db_session, incident)
