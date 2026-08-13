"""Tests the graph's routing/control-flow only (fan-out, retry loop,
resume-from-state, skip-unimplemented-investigators) — every agent
function is mocked here since each one already has its own test suite
covering its actual business logic.
"""

from datetime import datetime, timezone

import pytest

from app.graph import run_incident
from app.models import AuditLogEntry, Incident, IncidentSource, IncidentState


def _incident(state=IncidentState.DETECTED, **overrides):
    defaults = dict(
        source=IncidentSource.GITHUB_ACTIONS,
        external_id="run-1",
        title="CI failure: build on main",
        state=state,
        detected_at=datetime.now(timezone.utc),
        raw_payload={},
    )
    return Incident(**{**defaults, **overrides})


def _set_state(db, incident, state):
    incident.state = state
    db.commit()


def _fake_plan(investigators):
    def plan(db, incident):
        db.add(
            AuditLogEntry(
                incident_id=incident.id,
                actor="planner",
                action="investigators_selected",
                detail={"investigators": investigators, "reasoning": "test"},
            )
        )
        incident.state = IncidentState.INVESTIGATING
        db.commit()

    return plan


def _fail_if_called(name):
    def fn(db, incident):
        pytest.fail(f"{name} should not have been called")

    return fn


def test_run_incident_full_happy_path_runs_investigators_in_parallel(db_session, monkeypatch):
    incident = _incident(external_id="run-1")
    db_session.add(incident)
    db_session.flush()

    calls = {"log": 0, "git_diff": 0}
    monkeypatch.setattr("app.graph.plan_investigation", _fake_plan(["log", "git_diff"]))
    monkeypatch.setattr(
        "app.graph.log_investigate", lambda db, inc: calls.__setitem__("log", calls["log"] + 1)
    )
    monkeypatch.setattr(
        "app.graph.git_diff_investigate",
        lambda db, inc: calls.__setitem__("git_diff", calls["git_diff"] + 1),
    )
    monkeypatch.setattr(
        "app.graph.decide", lambda db, inc: _set_state(db, inc, IncidentState.FIX_PROPOSED)
    )
    monkeypatch.setattr(
        "app.graph.recover", lambda db, inc: _set_state(db, inc, IncidentState.VERIFYING)
    )
    monkeypatch.setattr(
        "app.graph.verify", lambda db, inc: _set_state(db, inc, IncidentState.AWAITING_APPROVAL)
    )

    result = run_incident(db_session, incident.id)

    assert result.state == IncidentState.AWAITING_APPROVAL
    assert calls == {"log": 1, "git_diff": 1}


def test_run_incident_stops_at_decision_on_low_confidence(db_session, monkeypatch):
    incident = _incident(external_id="run-2")
    db_session.add(incident)
    db_session.flush()

    monkeypatch.setattr("app.graph.plan_investigation", _fake_plan([]))
    monkeypatch.setattr(
        "app.graph.decide", lambda db, inc: _set_state(db, inc, IncidentState.NEEDS_HUMAN_TRIAGE)
    )
    monkeypatch.setattr("app.graph.recover", _fail_if_called("recover"))
    monkeypatch.setattr("app.graph.verify", _fail_if_called("verify"))

    result = run_incident(db_session, incident.id)

    assert result.state == IncidentState.NEEDS_HUMAN_TRIAGE


def test_run_incident_retries_diagnosis_after_verification_failure(db_session, monkeypatch):
    incident = _incident(external_id="run-3")
    db_session.add(incident)
    db_session.flush()

    calls = {"decide": 0, "recover": 0, "verify": 0}

    def fake_decide(db, inc):
        calls["decide"] += 1
        _set_state(db, inc, IncidentState.FIX_PROPOSED)

    def fake_recover(db, inc):
        calls["recover"] += 1
        _set_state(db, inc, IncidentState.VERIFYING)

    def fake_verify(db, inc):
        calls["verify"] += 1
        first_pass = calls["verify"] == 1
        outcome = IncidentState.DIAGNOSING if first_pass else IncidentState.AWAITING_APPROVAL
        _set_state(db, inc, outcome)

    monkeypatch.setattr("app.graph.plan_investigation", _fake_plan([]))
    monkeypatch.setattr("app.graph.decide", fake_decide)
    monkeypatch.setattr("app.graph.recover", fake_recover)
    monkeypatch.setattr("app.graph.verify", fake_verify)

    result = run_incident(db_session, incident.id)

    assert result.state == IncidentState.AWAITING_APPROVAL
    assert calls == {"decide": 2, "recover": 2, "verify": 2}


def test_run_incident_resumes_from_diagnosing_skips_planner(db_session, monkeypatch):
    incident = _incident(state=IncidentState.DIAGNOSING, external_id="run-4")
    db_session.add(incident)
    db_session.flush()

    monkeypatch.setattr("app.graph.plan_investigation", _fail_if_called("planner"))
    monkeypatch.setattr("app.graph.log_investigate", _fail_if_called("log_investigator"))
    monkeypatch.setattr("app.graph.git_diff_investigate", _fail_if_called("git_diff_investigator"))
    monkeypatch.setattr(
        "app.graph.decide", lambda db, inc: _set_state(db, inc, IncidentState.FIX_PROPOSED)
    )
    monkeypatch.setattr(
        "app.graph.recover", lambda db, inc: _set_state(db, inc, IncidentState.VERIFYING)
    )
    monkeypatch.setattr(
        "app.graph.verify", lambda db, inc: _set_state(db, inc, IncidentState.AWAITING_APPROVAL)
    )

    result = run_incident(db_session, incident.id)

    assert result.state == IncidentState.AWAITING_APPROVAL


def test_run_incident_resumes_from_verifying_and_stays_pending(db_session, monkeypatch):
    incident = _incident(state=IncidentState.VERIFYING, external_id="run-5")
    db_session.add(incident)
    db_session.flush()

    monkeypatch.setattr("app.graph.plan_investigation", _fail_if_called("planner"))
    monkeypatch.setattr("app.graph.decide", _fail_if_called("decide"))
    monkeypatch.setattr("app.graph.recover", _fail_if_called("recover"))

    verify_called = []
    # No state change on this mock — represents CI still running.
    monkeypatch.setattr("app.graph.verify", lambda db, inc: verify_called.append(True))

    result = run_incident(db_session, incident.id)

    assert verify_called == [True]
    assert result.state == IncidentState.VERIFYING  # unchanged — still pending


def test_run_incident_ends_immediately_on_terminal_state(db_session, monkeypatch):
    incident = _incident(state=IncidentState.MERGED, external_id="run-6")
    db_session.add(incident)
    db_session.flush()

    for name in ["plan_investigation", "decide", "recover", "verify"]:
        monkeypatch.setattr(f"app.graph.{name}", _fail_if_called(name))

    result = run_incident(db_session, incident.id)

    assert result.state == IncidentState.MERGED


def test_run_incident_skips_unimplemented_investigators(db_session, monkeypatch):
    incident = _incident(external_id="run-7")
    db_session.add(incident)
    db_session.flush()

    log_calls = []
    monkeypatch.setattr("app.graph.plan_investigation", _fake_plan(["log", "database"]))
    monkeypatch.setattr("app.graph.log_investigate", lambda db, inc: log_calls.append(True))
    monkeypatch.setattr("app.graph.git_diff_investigate", _fail_if_called("git_diff_investigator"))
    monkeypatch.setattr(
        "app.graph.decide", lambda db, inc: _set_state(db, inc, IncidentState.NEEDS_HUMAN_TRIAGE)
    )

    result = run_incident(db_session, incident.id)

    assert log_calls == [True]
    assert result.state == IncidentState.NEEDS_HUMAN_TRIAGE


def test_run_incident_routes_directly_to_decision_when_none_selected(db_session, monkeypatch):
    incident = _incident(external_id="run-8")
    db_session.add(incident)
    db_session.flush()

    decide_calls = []
    monkeypatch.setattr("app.graph.plan_investigation", _fake_plan([]))
    monkeypatch.setattr("app.graph.log_investigate", _fail_if_called("log_investigator"))
    monkeypatch.setattr("app.graph.git_diff_investigate", _fail_if_called("git_diff_investigator"))

    def fake_decide(db, inc):
        decide_calls.append(True)
        _set_state(db, inc, IncidentState.NEEDS_HUMAN_TRIAGE)

    monkeypatch.setattr("app.graph.decide", fake_decide)

    run_incident(db_session, incident.id)

    assert decide_calls == [True]
