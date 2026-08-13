"""One module per MVP failure source, each polled/consumed by the Incident
Watcher: github_actions, api_health, app_logs, docker_source, db_connectivity.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.models import IncidentSource


@dataclass(frozen=True)
class RawFailure:
    """Normalized shape every source returns. Watcher converts this into an
    Incident row; sources never touch the database directly.
    """

    source: IncidentSource
    external_id: str
    title: str
    detected_at: datetime
    raw_payload: dict

