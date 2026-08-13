import json
from datetime import datetime, timezone

import pytest

from app.agents.decision import decide
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


def _add_evidence(db_session, incident, actor, detail):
    db_session.add(
        AuditLogEntry(
            incident_id=incident.id, actor=actor, action="evidence_gathered", detail=detail
        )
    )
    db_session.flush()


def test_decide_high_confidence_sets_fix_proposed(db_session, monkeypatch):
    incident = _incident()
    db_session.add(incident)
    db_session.flush()
    _add_evidence(
        db_session, incident, "git_diff_investigator", {"commits": [{"message": "bump requests"}]}
    )

    response = {
        "root_cause": "Dependency bump removed a transitive package.",
        "confidence": 0.9,
        "recovery_strategy": "rollback",
        "reasoning": "Commit clearly correlates with failure onset.",
    }
    monkeypatch.setattr("app.agents.decision.complete", lambda system, user: json.dumps(response))

    result = decide(db_session, incident)

    assert result["recovery_strategy"] == "rollback"
    assert incident.state == IncidentState.FIX_PROPOSED

    entry = (
        db_session.query(AuditLogEntry)
        .filter(AuditLogEntry.incident_id == incident.id, AuditLogEntry.actor == "decision")
        .one()
    )
    assert entry.action == "diagnosis_completed"
    assert entry.detail["confidence"] == 0.9
    assert entry.detail["confidence_threshold"] == 0.7


def test_decide_low_confidence_sets_needs_human_triage(db_session, monkeypatch):
    incident = _incident(external_id="run-2")
    db_session.add(incident)
    db_session.flush()

    response = {
        "root_cause": "Unclear — evidence is inconclusive.",
        "confidence": 0.3,
        "recovery_strategy": "patch",
        "reasoning": "Not enough evidence to be confident.",
    }
    monkeypatch.setattr("app.agents.decision.complete", lambda system, user: json.dumps(response))

    result = decide(db_session, incident)

    assert result["confidence"] == 0.3
    assert incident.state == IncidentState.NEEDS_HUMAN_TRIAGE


def test_decide_raises_on_invalid_confidence(db_session, monkeypatch):
    incident = _incident(external_id="run-3")
    db_session.add(incident)
    db_session.flush()

    response = {
        "root_cause": "x",
        "confidence": 1.5,
        "recovery_strategy": "patch",
        "reasoning": "x",
    }
    monkeypatch.setattr("app.agents.decision.complete", lambda system, user: json.dumps(response))

    with pytest.raises(ValueError, match="invalid confidence"):
        decide(db_session, incident)


def test_decide_raises_on_invalid_strategy(db_session, monkeypatch):
    incident = _incident(external_id="run-4")
    db_session.add(incident)
    db_session.flush()

    response = {"root_cause": "x", "confidence": 0.8, "recovery_strategy": "reboot_the_universe"}
    monkeypatch.setattr("app.agents.decision.complete", lambda system, user: json.dumps(response))

    with pytest.raises(ValueError, match="invalid recovery_strategy"):
        decide(db_session, incident)


def test_decide_raises_on_missing_root_cause(db_session, monkeypatch):
    incident = _incident(external_id="run-5")
    db_session.add(incident)
    db_session.flush()

    response = {"confidence": 0.8, "recovery_strategy": "patch"}
    monkeypatch.setattr("app.agents.decision.complete", lambda system, user: json.dumps(response))

    with pytest.raises(ValueError, match="no root_cause"):
        decide(db_session, incident)


def test_decide_raises_on_invalid_json(db_session, monkeypatch):
    incident = _incident(external_id="run-6")
    db_session.add(incident)
    db_session.flush()

    monkeypatch.setattr("app.agents.decision.complete", lambda system, user: "not json")

    with pytest.raises(ValueError, match="did not return valid JSON"):
        decide(db_session, incident)
