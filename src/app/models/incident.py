import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class IncidentSource(str, enum.Enum):
    GITHUB_ACTIONS = "github_actions"
    API_HEALTH = "api_health"
    APP_LOGS = "app_logs"
    DOCKER = "docker"
    DATABASE = "database"


class IncidentState(str, enum.Enum):
    DETECTED = "detected"
    TRIAGING = "triaging"
    INVESTIGATING = "investigating"
    DIAGNOSING = "diagnosing"
    NEEDS_HUMAN_TRIAGE = "needs_human_triage"
    FIX_PROPOSED = "fix_proposed"
    VERIFYING = "verifying"
    ESCALATED = "escalated"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    MERGED = "merged"
    CLOSED = "closed"


class Incident(Base):
    """A normalized failure event. `source` + `external_id` is unique at the
    DB level so re-polling a source can never create a duplicate incident,
    even if the Watcher's own dedup check races with itself.
    """

    __tablename__ = "incidents"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_incidents_source_external_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source: Mapped[IncidentSource] = mapped_column(
        # values_callable: without it, SQLAlchemy sends the Python enum
        # MEMBER NAME ("GITHUB_ACTIONS") to Postgres, not `.value`
        # ("github_actions") — mismatches the lowercase values the enum
        # type was created with in the migration.
        Enum(IncidentSource, name="incident_source", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    external_id: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[IncidentState] = mapped_column(
        Enum(IncidentState, name="incident_state", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=IncidentState.DETECTED,
    )
    raw_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
