from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.models import AuditLogEntry, Incident, IncidentSource
from app.sources import RawFailure
from app.sources.api_health import check_api_health
from app.sources.app_logs import scan_error_logs
from app.sources.github_actions import fetch_failed_workflow_runs


def ingest_failures(db: Session, raw_failures: list[RawFailure]) -> list[Incident]:
    """Persist whichever `raw_failures` aren't already known Incidents, each
    with an audit_log entry. Source-agnostic — every MVP source's failures
    flow through this same function once they're normalized to RawFailure.
    """
    if not raw_failures:
        return []

    source = raw_failures[0].source
    candidate_ids = [f.external_id for f in raw_failures]
    existing_ids = {
        row.external_id
        for row in db.query(Incident.external_id)
        .filter(Incident.source == source)
        .filter(Incident.external_id.in_(candidate_ids))
    }

    created: list[Incident] = []
    for failure in raw_failures:
        if failure.external_id in existing_ids:
            continue

        incident = Incident(
            source=failure.source,
            external_id=failure.external_id,
            title=failure.title,
            detected_at=failure.detected_at,
            raw_payload=failure.raw_payload,
        )
        db.add(incident)
        db.flush()  # populate incident.id (client-side default) for the audit FK

        db.add(
            AuditLogEntry(
                incident_id=incident.id,
                actor="watcher",
                action="incident_detected",
                detail={"source": failure.source.value, "external_id": failure.external_id},
            )
        )
        created.append(incident)

    db.commit()
    return created


def poll_github_actions(db: Session, since: datetime) -> list[Incident]:
    raw_failures = fetch_failed_workflow_runs(since)
    return ingest_failures(db, raw_failures)


def poll_api_health(db: Session) -> list[Incident]:
    raw_failures = check_api_health()
    return ingest_failures(db, raw_failures)


def poll_app_logs(db: Session, since: datetime) -> list[Incident]:
    raw_failures = scan_error_logs(since)
    return ingest_failures(db, raw_failures)
