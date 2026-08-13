from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models import IncidentSource, IncidentState


class IncidentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source: IncidentSource
    external_id: str
    title: str
    state: IncidentState
    detected_at: datetime
    created_at: datetime
