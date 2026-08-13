"""Human Approval gate: the required checkpoint before any merge or other
production-impacting action (CLAUDE.md safety rule — not just a log
message, an explicit code-level guard). Nothing in this codebase advances
an incident past AWAITING_APPROVAL on its own; approve/reject/merge are
only ever invoked via the API endpoints in api/routes.py, i.e. by a human.
"""

from __future__ import annotations

from github import Github
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AuditLogEntry, Incident, IncidentState


def approve(db: Session, incident: Incident, approved_by: str) -> None:
    """Records human approval. Does not merge — call `merge` separately
    (the API route does both in one request) so a failed merge can be
    retried without re-approving.
    """
    _require_state(incident, IncidentState.AWAITING_APPROVAL)
    incident.state = IncidentState.APPROVED
    db.add(
        AuditLogEntry(incident_id=incident.id, actor=approved_by, action="approved", detail={})
    )
    db.commit()


def reject(db: Session, incident: Incident, rejected_by: str, reason: str = "") -> None:
    _require_state(incident, IncidentState.AWAITING_APPROVAL)
    incident.state = IncidentState.REJECTED
    db.add(
        AuditLogEntry(
            incident_id=incident.id,
            actor=rejected_by,
            action="rejected",
            detail={"reason": reason},
        )
    )
    db.commit()


def merge(db: Session, incident: Incident, client: Github | None = None) -> dict:
    """Merges the Recovery Agent's fix PR. Guarded on APPROVED state — this
    guard is the actual enforcement of "no automatic merge without human
    approval", not a convention callers are trusted to follow.
    """
    _require_state(incident, IncidentState.APPROVED)

    fix_entry = _latest_fix_proposed(db, incident)
    if fix_entry is None:
        raise ValueError(f"No fix_proposed found for incident {incident.id}")
    pr_number = fix_entry.detail["pr_number"]

    if not settings.github_token:
        raise RuntimeError("GITHUB_TOKEN is not configured")
    if not settings.github_repo:
        raise RuntimeError("GITHUB_REPO is not configured")

    gh = client or Github(settings.github_token)
    repo = gh.get_repo(settings.github_repo)
    result = repo.get_pull(pr_number).merge()

    incident.state = IncidentState.MERGED
    db.add(
        AuditLogEntry(
            incident_id=incident.id,
            actor="human_approval",
            action="merged",
            detail={"pr_number": pr_number, "merged": result.merged, "sha": result.sha},
        )
    )
    db.commit()
    return {"merged": result.merged, "sha": result.sha}


def _require_state(incident: Incident, required: IncidentState) -> None:
    if incident.state != required:
        raise ValueError(
            f"Incident {incident.id} is {incident.state.value}, expected {required.value}"
        )


def _latest_fix_proposed(db: Session, incident: Incident) -> AuditLogEntry | None:
    return (
        db.query(AuditLogEntry)
        .filter(
            AuditLogEntry.incident_id == incident.id,
            AuditLogEntry.actor == "recovery",
            AuditLogEntry.action == "fix_proposed",
        )
        .order_by(AuditLogEntry.created_at.desc())
        .first()
    )
