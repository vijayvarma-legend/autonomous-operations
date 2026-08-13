from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from app.agents import human_approval
from app.models import AuditLogEntry, Incident, IncidentSource, IncidentState

DETECTED_AT = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)


@dataclass
class FakeMergeResult:
    merged: bool
    sha: str


class FakePR:
    def merge(self):
        return FakeMergeResult(merged=True, sha="deadbeef")


class FakeRepo:
    def __init__(self):
        self.requested_pr_number = None

    def get_pull(self, number):
        self.requested_pr_number = number
        return FakePR()


class FakeGithub:
    def __init__(self, repo):
        self._repo = repo

    def get_repo(self, name):
        return self._repo


def _incident(state=IncidentState.AWAITING_APPROVAL, **overrides):
    defaults = dict(
        source=IncidentSource.GITHUB_ACTIONS,
        external_id="run-1",
        title="CI failure: build on main",
        state=state,
        detected_at=DETECTED_AT,
        raw_payload={},
    )
    return Incident(**{**defaults, **overrides})


def _add_fix_proposed(db_session, incident, pr_number=42):
    db_session.add(
        AuditLogEntry(
            incident_id=incident.id,
            actor="recovery",
            action="fix_proposed",
            detail={"branch": "auto-fix/x-1", "pr_number": pr_number},
        )
    )
    db_session.flush()


# --- approve ---


def test_approve_sets_approved_and_records_audit(db_session):
    incident = _incident()
    db_session.add(incident)
    db_session.flush()

    human_approval.approve(db_session, incident, approved_by="alice@example.com")

    assert incident.state == IncidentState.APPROVED
    entry = (
        db_session.query(AuditLogEntry)
        .filter(AuditLogEntry.incident_id == incident.id, AuditLogEntry.action == "approved")
        .one()
    )
    assert entry.actor == "alice@example.com"


def test_approve_rejects_wrong_state(db_session):
    incident = _incident(state=IncidentState.DETECTED, external_id="run-2")
    db_session.add(incident)
    db_session.flush()

    with pytest.raises(ValueError, match="expected awaiting_approval"):
        human_approval.approve(db_session, incident, approved_by="alice@example.com")


# --- reject ---


def test_reject_sets_rejected_and_records_audit(db_session):
    incident = _incident(external_id="run-3")
    db_session.add(incident)
    db_session.flush()

    human_approval.reject(db_session, incident, rejected_by="bob@example.com", reason="not safe")

    assert incident.state == IncidentState.REJECTED
    entry = (
        db_session.query(AuditLogEntry)
        .filter(AuditLogEntry.incident_id == incident.id, AuditLogEntry.action == "rejected")
        .one()
    )
    assert entry.actor == "bob@example.com"
    assert entry.detail["reason"] == "not safe"


def test_reject_rejects_wrong_state(db_session):
    incident = _incident(state=IncidentState.DETECTED, external_id="run-4")
    db_session.add(incident)
    db_session.flush()

    with pytest.raises(ValueError):
        human_approval.reject(db_session, incident, rejected_by="bob@example.com")


# --- merge ---


def test_merge_requires_approved_state(db_session, monkeypatch):
    monkeypatch.setattr("app.agents.human_approval.settings.github_token", "tok")
    monkeypatch.setattr("app.agents.human_approval.settings.github_repo", "owner/repo")
    incident = _incident(state=IncidentState.AWAITING_APPROVAL, external_id="run-5")
    db_session.add(incident)
    db_session.flush()

    with pytest.raises(ValueError, match="expected approved"):
        human_approval.merge(db_session, incident)


def test_merge_calls_github_and_sets_merged(db_session, monkeypatch):
    monkeypatch.setattr("app.agents.human_approval.settings.github_token", "tok")
    monkeypatch.setattr("app.agents.human_approval.settings.github_repo", "owner/repo")
    incident = _incident(state=IncidentState.APPROVED, external_id="run-6")
    db_session.add(incident)
    db_session.flush()
    _add_fix_proposed(db_session, incident, pr_number=99)

    repo = FakeRepo()
    client = FakeGithub(repo)

    result = human_approval.merge(db_session, incident, client=client)

    assert result["merged"] is True
    assert result["sha"] == "deadbeef"
    assert incident.state == IncidentState.MERGED
    assert repo.requested_pr_number == 99

    entry = (
        db_session.query(AuditLogEntry)
        .filter(AuditLogEntry.incident_id == incident.id, AuditLogEntry.action == "merged")
        .one()
    )
    assert entry.actor == "human_approval"
    assert entry.detail["pr_number"] == 99


def test_merge_raises_without_fix_proposed(db_session, monkeypatch):
    monkeypatch.setattr("app.agents.human_approval.settings.github_token", "tok")
    monkeypatch.setattr("app.agents.human_approval.settings.github_repo", "owner/repo")
    incident = _incident(state=IncidentState.APPROVED, external_id="run-7")
    db_session.add(incident)
    db_session.flush()

    with pytest.raises(ValueError, match="No fix_proposed found"):
        human_approval.merge(db_session, incident)


def test_merge_requires_github_token(db_session, monkeypatch):
    monkeypatch.setattr("app.agents.human_approval.settings.github_token", "")
    monkeypatch.setattr("app.agents.human_approval.settings.github_repo", "owner/repo")
    incident = _incident(state=IncidentState.APPROVED, external_id="run-8")
    db_session.add(incident)
    db_session.flush()
    _add_fix_proposed(db_session, incident)

    with pytest.raises(RuntimeError, match="GITHUB_TOKEN"):
        human_approval.merge(db_session, incident)


def test_merge_requires_github_repo(db_session, monkeypatch):
    monkeypatch.setattr("app.agents.human_approval.settings.github_token", "tok")
    monkeypatch.setattr("app.agents.human_approval.settings.github_repo", "")
    incident = _incident(state=IncidentState.APPROVED, external_id="run-9")
    db_session.add(incident)
    db_session.flush()
    _add_fix_proposed(db_session, incident)

    with pytest.raises(RuntimeError, match="GITHUB_REPO"):
        human_approval.merge(db_session, incident)
