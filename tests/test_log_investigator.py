from datetime import datetime, timezone

import pytest

from app.agents.investigators.log_investigator import investigate
from app.models import AuditLogEntry, Incident, IncidentSource

DETECTED_AT = datetime(2026, 8, 13, 10, 15, 0, tzinfo=timezone.utc)


def _incident(**overrides):
    defaults = dict(
        source=IncidentSource.API_HEALTH,
        external_id="incident-1",
        title="5xx spike on /users",
        detected_at=DETECTED_AT,
        raw_payload={},
    )
    return Incident(**{**defaults, **overrides})


def test_investigate_collects_entries_within_window_at_any_level(db_session):
    incident = _incident()
    db_session.add(incident)
    db_session.flush()

    lines = [
        "2026-08-13 09:55:00,000 ERROR app.db: too far before window, excluded",
        "2026-08-13 10:05:00,000 ERROR app.db: connection pool exhausted",
        "2026-08-13 10:06:00,000 INFO app.api.users: handling request /users/42",
        "2026-08-13 10:18:00,000 CRITICAL app.api.users: unhandled exception",
        "2026-08-13 10:25:00,000 ERROR app.api.users: too far after window, excluded",
    ]

    result = investigate(db_session, incident, lines=lines)

    assert [(e["level"], e["message"]) for e in result] == [
        ("ERROR", "connection pool exhausted"),
        ("INFO", "handling request /users/42"),
        ("CRITICAL", "unhandled exception"),
    ]


def test_investigate_records_audit_entry(db_session):
    incident = _incident(external_id="incident-2")
    db_session.add(incident)
    db_session.flush()

    lines = ["2026-08-13 10:10:00,000 ERROR app.db: boom"]

    investigate(db_session, incident, lines=lines)

    entry = (
        db_session.query(AuditLogEntry)
        .filter(AuditLogEntry.incident_id == incident.id, AuditLogEntry.actor == "log_investigator")
        .one()
    )
    assert entry.action == "evidence_gathered"
    assert len(entry.detail["log_entries"]) == 1
    assert entry.detail["log_entries"][0]["message"] == "boom"
    assert entry.detail["window_start"] == "2026-08-13T10:00:00+00:00"
    assert entry.detail["window_end"] == "2026-08-13T10:20:00+00:00"


def test_investigate_returns_empty_list_when_no_matches(db_session):
    incident = _incident(external_id="incident-3")
    db_session.add(incident)
    db_session.flush()

    result = investigate(db_session, incident, lines=["2026-08-13 09:00:00,000 ERROR x: too old"])

    assert result == []


def test_investigate_requires_app_log_path_when_no_lines_given(db_session, monkeypatch):
    incident = _incident(external_id="incident-4")
    db_session.add(incident)
    db_session.flush()

    monkeypatch.setattr("app.sources.app_logs.settings.app_log_path", "")

    with pytest.raises(RuntimeError, match="APP_LOG_PATH"):
        investigate(db_session, incident)
