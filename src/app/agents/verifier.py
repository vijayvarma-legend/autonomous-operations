"""Verifier Agent: runs tests/health checks against a Recovery Agent fix.
On failure, sends new evidence back to the Decision Agent for another
diagnosis cycle. The retry loop is bounded by MAX_RECOVERY_ATTEMPTS, which
the Recovery Agent enforces on its own attempt count — Verifier doesn't
need a separate cap, it just reports pass/fail.
"""

from __future__ import annotations

from github import Github
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AuditLogEntry, Incident, IncidentState


def verify(db: Session, incident: Incident, client: Github | None = None) -> dict:
    """Checks the CI run for the branch the Recovery Agent proposed a fix
    on. success -> AWAITING_APPROVAL (ready for the human gate). Anything
    else that completed -> DIAGNOSING, with the failure recorded as
    evidence for the Decision Agent's next pass. Still running / no run
    yet -> no state change, caller should check again later.
    """
    fix_entry = _latest_fix_proposed(db, incident)
    if fix_entry is None:
        raise ValueError(
            f"No fix_proposed found for incident {incident.id}; run the Recovery Agent first"
        )
    branch = fix_entry.detail["branch"]

    if not settings.github_token:
        raise RuntimeError("GITHUB_TOKEN is not configured")
    if not settings.github_repo:
        raise RuntimeError("GITHUB_REPO is not configured")

    gh = client or Github(settings.github_token)
    repo = gh.get_repo(settings.github_repo)
    run = _latest_workflow_run(repo, branch)

    if run is None or run.status != "completed":
        _record(db, incident, "verification_pending", {"branch": branch})
        db.commit()
        return {"status": "pending", "branch": branch}

    passed = run.conclusion == "success"
    detail = {
        "branch": branch,
        "run_id": run.id,
        "conclusion": run.conclusion,
        "html_url": run.html_url,
    }

    if passed:
        incident.state = IncidentState.AWAITING_APPROVAL
        _record(db, incident, "verification_passed", detail)
    else:
        incident.state = IncidentState.DIAGNOSING
        _record(db, incident, "verification_failed", detail)

    db.commit()
    return {
        "status": "passed" if passed else "failed",
        "run_id": run.id,
        "conclusion": run.conclusion,
    }


def _latest_workflow_run(repo, branch: str):
    for run in repo.get_workflow_runs(branch=branch):
        return run  # newest-first, same convention as app.sources.github_actions
    return None


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


def _record(db: Session, incident: Incident, action: str, detail: dict) -> None:
    db.add(AuditLogEntry(incident_id=incident.id, actor="verifier", action=action, detail=detail))
