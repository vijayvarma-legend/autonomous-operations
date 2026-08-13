from datetime import datetime, timezone

from app.agents.watcher import ingest_failures
from app.models import AuditLogEntry, Incident, IncidentSource
from app.sources.github_actions import RawFailure


def _failure(external_id: str) -> RawFailure:
    return RawFailure(
        source=IncidentSource.GITHUB_ACTIONS,
        external_id=external_id,
        title=f"CI failure: build on main ({external_id})",
        detected_at=datetime.now(timezone.utc),
        raw_payload={"run_id": external_id},
    )


def test_ingest_failures_creates_incidents_and_audit_entries(db_session):
    created = ingest_failures(db_session, [_failure("run-100"), _failure("run-101")])

    assert {i.external_id for i in created} == {"run-100", "run-101"}

    stored = (
        db_session.query(Incident)
        .filter(Incident.external_id.in_(["run-100", "run-101"]))
        .all()
    )
    assert len(stored) == 2
    assert all(i.source == IncidentSource.GITHUB_ACTIONS for i in stored)

    audit_entries = (
        db_session.query(AuditLogEntry)
        .filter(AuditLogEntry.incident_id.in_([i.id for i in stored]))
        .all()
    )
    assert len(audit_entries) == 2
    assert all(a.actor == "watcher" and a.action == "incident_detected" for a in audit_entries)


def test_ingest_failures_dedupes_against_existing(db_session):
    ingest_failures(db_session, [_failure("run-200")])

    second_pass = ingest_failures(db_session, [_failure("run-200"), _failure("run-201")])

    assert {i.external_id for i in second_pass} == {"run-201"}
    stored = (
        db_session.query(Incident)
        .filter(Incident.external_id.in_(["run-200", "run-201"]))
        .all()
    )
    assert len(stored) == 2  # run-200 exists exactly once, not duplicated


def test_ingest_failures_empty_list_returns_empty(db_session):
    assert ingest_failures(db_session, []) == []
