"""Log Investigator: searches application logs for evidence relevant to an
incident. Deterministic evidence gathering — no LLM call here; that's the
Decision Agent's job once all investigators have reported in.

Unlike app_logs (which only flags ERROR/CRITICAL to detect new incidents),
this pulls every log level within the window — the INFO/WARNING lines
right before an error are often what explains it (a request starting, a
job kicking off, a config reload), and the Decision Agent needs that
lead-up, not just the error line itself.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta

from sqlalchemy.orm import Session

from app.models import AuditLogEntry, Incident
from app.sources.app_logs import parse_log_lines, read_configured_log_lines

# Wide enough to catch the root-cause error that preceded the incident and
# any immediate fallout, without pulling in unrelated noise from the log.
WINDOW_BEFORE = timedelta(minutes=15)
WINDOW_AFTER = timedelta(minutes=5)


def investigate(db: Session, incident: Incident, lines: Iterable[str] | None = None) -> list[dict]:
    """Collects log lines of any level within a window around
    incident.detected_at, records them as evidence on the audit trail, and
    returns them for the Decision Agent to consume.
    """
    if lines is None:
        lines = read_configured_log_lines()

    window_start = incident.detected_at - WINDOW_BEFORE
    window_end = incident.detected_at + WINDOW_AFTER

    entries = [
        {
            "timestamp": p.timestamp.isoformat(),
            "logger": p.logger,
            "level": p.level,
            "message": p.message,
        }
        for p in parse_log_lines(lines)
        if window_start <= p.timestamp <= window_end
    ]

    db.add(
        AuditLogEntry(
            incident_id=incident.id,
            actor="log_investigator",
            action="evidence_gathered",
            detail={
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
                "log_entries": entries,
            },
        )
    )
    db.commit()
    return entries
