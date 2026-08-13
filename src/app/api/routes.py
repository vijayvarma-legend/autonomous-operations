"""HTTP routes beyond /health. Grows as agents need endpoints
(e.g. approval actions) as they land.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.agents import human_approval
from app.api.schemas import ApprovalRequest, IncidentOut, RejectionRequest
from app.db import get_db
from app.models import Incident, IncidentState

router = APIRouter()


@router.get("/incidents", response_model=list[IncidentOut])
def list_incidents(
    state: IncidentState | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[Incident]:
    query = db.query(Incident).order_by(Incident.detected_at.desc())
    if state is not None:
        query = query.filter(Incident.state == state)
    return query.all()


def _get_incident_or_404(db: Session, incident_id: uuid.UUID) -> Incident:
    incident = db.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    return incident


@router.post("/incidents/{incident_id}/approve", response_model=IncidentOut)
def approve_incident(
    incident_id: uuid.UUID,
    body: ApprovalRequest,
    db: Session = Depends(get_db),
) -> Incident:
    """The human approval gate: records approval, then immediately merges
    the fix PR. This is the only code path in the system that can merge a
    Recovery Agent fix — see app.agents.human_approval for the guard.
    """
    incident = _get_incident_or_404(db, incident_id)
    try:
        human_approval.approve(db, incident, approved_by=body.approved_by)
        human_approval.merge(db, incident)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return incident


@router.post("/incidents/{incident_id}/merge", response_model=IncidentOut)
def merge_incident(incident_id: uuid.UUID, db: Session = Depends(get_db)) -> Incident:
    """Retries the merge for an already-approved incident whose merge
    failed (e.g. a transient GitHub error) — does not require re-approval.
    """
    incident = _get_incident_or_404(db, incident_id)
    try:
        human_approval.merge(db, incident)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return incident


@router.post("/incidents/{incident_id}/reject", response_model=IncidentOut)
def reject_incident(
    incident_id: uuid.UUID,
    body: RejectionRequest,
    db: Session = Depends(get_db),
) -> Incident:
    incident = _get_incident_or_404(db, incident_id)
    try:
        human_approval.reject(db, incident, rejected_by=body.rejected_by, reason=body.reason)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return incident
