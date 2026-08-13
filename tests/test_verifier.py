from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from app.agents.verifier import verify
from app.models import AuditLogEntry, Incident, IncidentSource, IncidentState

DETECTED_AT = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)


@dataclass
class FakeRun:
    id: int
    status: str
    conclusion: str | None
    html_url: str = "https://github.com/x/y/actions/runs/1"


class FakeRepo:
    def __init__(self, runs):
        self._runs = runs
        self.requested_branch = None

    def get_workflow_runs(self, branch=None):
        self.requested_branch = branch
        return self._runs


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


def _add_fix_proposed(db_session, incident, branch="auto-fix/x-1"):
    db_session.add(
        AuditLogEntry(
            incident_id=incident.id,
            actor="recovery",
            action="fix_proposed",
            detail={"branch": branch, "strategy": "patch"},
        )
    )
    db_session.flush()


def _set_github_config(monkeypatch, token="tok", repo="owner/repo"):
    monkeypatch.setattr("app.agents.verifier.settings.github_token", token)
    monkeypatch.setattr("app.agents.verifier.settings.github_repo", repo)


def test_verify_passes_moves_to_awaiting_approval(db_session, monkeypatch):
    _set_github_config(monkeypatch)
    incident = _incident()
    db_session.add(incident)
    db_session.flush()
    _add_fix_proposed(db_session, incident, branch="auto-fix/foo-1")

    repo = FakeRepo([FakeRun(id=1, status="completed", conclusion="success")])
    client = FakeGithub(repo)

    result = verify(db_session, incident, client=client)

    assert result["status"] == "passed"
    assert incident.state == IncidentState.AWAITING_APPROVAL
    assert repo.requested_branch == "auto-fix/foo-1"

    entry = (
        db_session.query(AuditLogEntry)
        .filter(AuditLogEntry.incident_id == incident.id, AuditLogEntry.actor == "verifier")
        .one()
    )
    assert entry.action == "verification_passed"


def test_verify_fails_moves_to_diagnosing_and_feeds_decision(db_session, monkeypatch):
    _set_github_config(monkeypatch)
    incident = _incident(external_id="run-2")
    db_session.add(incident)
    db_session.flush()
    _add_fix_proposed(db_session, incident, branch="auto-fix/foo-1")

    repo = FakeRepo([FakeRun(id=2, status="completed", conclusion="failure")])
    client = FakeGithub(repo)

    result = verify(db_session, incident, client=client)

    assert result["status"] == "failed"
    assert incident.state == IncidentState.DIAGNOSING

    entry = (
        db_session.query(AuditLogEntry)
        .filter(AuditLogEntry.incident_id == incident.id, AuditLogEntry.actor == "verifier")
        .one()
    )
    assert entry.action == "verification_failed"

    # This is what closes the retry loop: Decision Agent's evidence query
    # must pick up the failure without any special-casing on its part.
    from app.agents.decision import decide

    monkeypatch.setattr(
        "app.agents.decision.complete",
        lambda system, user: (
            '{"root_cause": "still broken", "confidence": 0.5, '
            '"recovery_strategy": "patch", "reasoning": "retry"}'
        ),
    )
    decide(db_session, incident)  # must not raise / must see the verifier's evidence


def test_verify_pending_when_run_not_completed(db_session, monkeypatch):
    _set_github_config(monkeypatch)
    incident = _incident(external_id="run-3")
    db_session.add(incident)
    db_session.flush()
    _add_fix_proposed(db_session, incident)

    repo = FakeRepo([FakeRun(id=3, status="in_progress", conclusion=None)])
    client = FakeGithub(repo)

    result = verify(db_session, incident, client=client)

    assert result["status"] == "pending"
    assert incident.state == IncidentState.DETECTED  # untouched


def test_verify_pending_when_no_run_found(db_session, monkeypatch):
    _set_github_config(monkeypatch)
    incident = _incident(external_id="run-4")
    db_session.add(incident)
    db_session.flush()
    _add_fix_proposed(db_session, incident)

    repo = FakeRepo([])
    client = FakeGithub(repo)

    result = verify(db_session, incident, client=client)

    assert result["status"] == "pending"


def test_verify_raises_without_fix_proposed(db_session, monkeypatch):
    _set_github_config(monkeypatch)
    incident = _incident(external_id="run-5")
    db_session.add(incident)
    db_session.flush()

    with pytest.raises(ValueError, match="No fix_proposed found"):
        verify(db_session, incident)


def test_verify_requires_github_token(db_session, monkeypatch):
    _set_github_config(monkeypatch, token="")
    incident = _incident(external_id="run-6")
    db_session.add(incident)
    db_session.flush()
    _add_fix_proposed(db_session, incident)

    with pytest.raises(RuntimeError, match="GITHUB_TOKEN"):
        verify(db_session, incident)


def test_verify_requires_github_repo(db_session, monkeypatch):
    _set_github_config(monkeypatch, repo="")
    incident = _incident(external_id="run-7")
    db_session.add(incident)
    db_session.flush()
    _add_fix_proposed(db_session, incident)

    with pytest.raises(RuntimeError, match="GITHUB_REPO"):
        verify(db_session, incident)
