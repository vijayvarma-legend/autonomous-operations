import json
from datetime import datetime, timezone

import pytest

from app.agents.planner import plan_investigation
from app.models import AuditLogEntry, Incident, IncidentSource, IncidentState


def _incident(**overrides):
    defaults = dict(
        source=IncidentSource.GITHUB_ACTIONS,
        external_id="run-1",
        title="CI failure: build on main",
        detected_at=datetime.now(timezone.utc),
        raw_payload={"run_id": 1},
    )
    return Incident(**{**defaults, **overrides})


def test_plan_investigation_selects_investigators_and_updates_state(db_session, monkeypatch):
    incident = _incident()
    db_session.add(incident)
    db_session.flush()

    response = {
        "investigators": ["git_diff", "log"],
        "reasoning": "CI failure, check recent commits and logs.",
    }
    monkeypatch.setattr("app.agents.planner.complete", lambda system, user: json.dumps(response))

    result = plan_investigation(db_session, incident)

    assert result == ["git_diff", "log"]
    assert incident.state == IncidentState.INVESTIGATING

    entry = (
        db_session.query(AuditLogEntry)
        .filter(AuditLogEntry.incident_id == incident.id, AuditLogEntry.actor == "planner")
        .one()
    )
    assert entry.action == "investigators_selected"
    assert entry.detail["investigators"] == ["git_diff", "log"]
    assert "reasoning" in entry.detail


def test_plan_investigation_drops_invalid_investigator_names(db_session, monkeypatch):
    incident = _incident(external_id="run-2")
    db_session.add(incident)
    db_session.flush()

    response = {"investigators": ["log", "not_a_real_investigator"], "reasoning": "x"}
    monkeypatch.setattr("app.agents.planner.complete", lambda system, user: json.dumps(response))

    result = plan_investigation(db_session, incident)

    assert result == ["log"]


def test_plan_investigation_raises_on_empty_selection(db_session, monkeypatch):
    incident = _incident(external_id="run-3")
    db_session.add(incident)
    db_session.flush()

    monkeypatch.setattr(
        "app.agents.planner.complete",
        lambda system, user: json.dumps({"investigators": [], "reasoning": "nothing relevant"}),
    )

    with pytest.raises(ValueError, match="no valid investigators"):
        plan_investigation(db_session, incident)


def test_plan_investigation_raises_on_invalid_json(db_session, monkeypatch):
    incident = _incident(external_id="run-4")
    db_session.add(incident)
    db_session.flush()

    monkeypatch.setattr("app.agents.planner.complete", lambda system, user: "not json")

    with pytest.raises(ValueError, match="did not return valid JSON"):
        plan_investigation(db_session, incident)
