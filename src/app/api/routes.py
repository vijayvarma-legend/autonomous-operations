"""HTTP routes beyond /health. Grows as agents need endpoints
(e.g. approval actions) as they land.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.schemas import IncidentOut
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
