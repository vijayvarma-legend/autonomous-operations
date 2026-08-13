import uuid
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.db import get_db
from app.main import app
from app.models import Incident, IncidentSource, IncidentState

client = TestClient(app)


def test_list_incidents_rejects_invalid_state() -> None:
    # Doesn't touch the DB: FastAPI validates the enum query param and
    # returns 422 before the route function (and its `Depends(get_db)`) runs.
    response = client.get("/incidents", params={"state": "not-a-real-state"})
    assert response.status_code == 422


def test_list_incidents_returns_created_incident(db_session) -> None:
    external_id = f"api-test-{uuid.uuid4()}"
    db_session.add(
        Incident(
            source=IncidentSource.GITHUB_ACTIONS,
            external_id=external_id,
            title="test incident",
            detected_at=datetime.now(timezone.utc),
            raw_payload={"foo": "bar"},
        )
    )
    db_session.commit()

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        response = client.get("/incidents")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    matches = [row for row in response.json() if row["external_id"] == external_id]
    assert len(matches) == 1
    assert matches[0]["source"] == "github_actions"
    assert matches[0]["state"] == "detected"


def test_list_incidents_filters_by_state(db_session) -> None:
    external_id = f"api-test-{uuid.uuid4()}"
    db_session.add(
        Incident(
            source=IncidentSource.GITHUB_ACTIONS,
            external_id=external_id,
            title="triaging incident",
            state=IncidentState.TRIAGING,
            detected_at=datetime.now(timezone.utc),
            raw_payload={},
        )
    )
    db_session.commit()

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        detected_response = client.get("/incidents", params={"state": "detected"})
        triaging_response = client.get("/incidents", params={"state": "triaging"})
    finally:
        app.dependency_overrides.clear()

    assert all(row["external_id"] != external_id for row in detected_response.json())
    triaging_matches = [
        row for row in triaging_response.json() if row["external_id"] == external_id
    ]
    assert len(triaging_matches) == 1
