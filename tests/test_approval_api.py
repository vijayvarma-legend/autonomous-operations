import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.db import get_db
from app.main import app
from app.models import AuditLogEntry, Incident, IncidentSource, IncidentState

client = TestClient(app)


@dataclass
class FakeMergeResult:
    merged: bool = True
    sha: str = "deadbeef"


class FakePR:
    def merge(self):
        return FakeMergeResult()


class FakeRepo:
    def get_pull(self, number):
        return FakePR()


class FakeGithub:
    def __init__(self, token):
        pass

    def get_repo(self, name):
        return FakeRepo()


def _incident(state, external_id=None):
    return Incident(
        source=IncidentSource.GITHUB_ACTIONS,
        external_id=external_id or f"api-test-{uuid.uuid4()}",
        title="test incident",
        state=state,
        detected_at=datetime.now(timezone.utc),
        raw_payload={},
    )


def _run_with_db_override(db_session, fn):
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        return fn()
    finally:
        app.dependency_overrides.clear()


def test_approve_nonexistent_incident_returns_404(db_session):
    response = _run_with_db_override(
        db_session,
        lambda: client.post(f"/incidents/{uuid.uuid4()}/approve", json={"approved_by": "alice"}),
    )
    assert response.status_code == 404


def test_approve_wrong_state_returns_409(db_session):
    incident = _incident(IncidentState.DETECTED)
    db_session.add(incident)
    db_session.commit()

    response = _run_with_db_override(
        db_session,
        lambda: client.post(
            f"/incidents/{incident.id}/approve", json={"approved_by": "alice"}
        ),
    )
    assert response.status_code == 409


def test_approve_merges_and_returns_merged_state(db_session, monkeypatch):
    monkeypatch.setattr("app.agents.human_approval.settings.github_token", "tok")
    monkeypatch.setattr("app.agents.human_approval.settings.github_repo", "owner/repo")
    monkeypatch.setattr("app.agents.human_approval.Github", FakeGithub)

    incident = _incident(IncidentState.AWAITING_APPROVAL)
    db_session.add(incident)
    db_session.flush()
    db_session.add(
        AuditLogEntry(
            incident_id=incident.id,
            actor="recovery",
            action="fix_proposed",
            detail={"branch": "auto-fix/x-1", "pr_number": 7},
        )
    )
    db_session.commit()

    response = _run_with_db_override(
        db_session,
        lambda: client.post(
            f"/incidents/{incident.id}/approve", json={"approved_by": "alice@example.com"}
        ),
    )

    assert response.status_code == 200
    assert response.json()["state"] == "merged"


def test_reject_sets_rejected_state(db_session):
    incident = _incident(IncidentState.AWAITING_APPROVAL)
    db_session.add(incident)
    db_session.commit()

    response = _run_with_db_override(
        db_session,
        lambda: client.post(
            f"/incidents/{incident.id}/reject",
            json={"rejected_by": "bob@example.com", "reason": "too risky"},
        ),
    )

    assert response.status_code == 200
    assert response.json()["state"] == "rejected"


def test_reject_wrong_state_returns_409(db_session):
    incident = _incident(IncidentState.DETECTED)
    db_session.add(incident)
    db_session.commit()

    response = _run_with_db_override(
        db_session,
        lambda: client.post(
            f"/incidents/{incident.id}/reject", json={"rejected_by": "bob@example.com"}
        ),
    )
    assert response.status_code == 409


def test_merge_endpoint_retries_after_approval(db_session, monkeypatch):
    monkeypatch.setattr("app.agents.human_approval.settings.github_token", "tok")
    monkeypatch.setattr("app.agents.human_approval.settings.github_repo", "owner/repo")
    monkeypatch.setattr("app.agents.human_approval.Github", FakeGithub)

    incident = _incident(IncidentState.APPROVED)
    db_session.add(incident)
    db_session.flush()
    db_session.add(
        AuditLogEntry(
            incident_id=incident.id,
            actor="recovery",
            action="fix_proposed",
            detail={"branch": "auto-fix/x-1", "pr_number": 7},
        )
    )
    db_session.commit()

    response = _run_with_db_override(
        db_session, lambda: client.post(f"/incidents/{incident.id}/merge")
    )

    assert response.status_code == 200
    assert response.json()["state"] == "merged"
