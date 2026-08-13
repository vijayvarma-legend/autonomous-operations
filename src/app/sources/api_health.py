from __future__ import annotations

from datetime import datetime, timezone

import httpx

from app.config import settings
from app.models import IncidentSource
from app.sources import RawFailure

_BUCKET_MINUTES = 5  # collapses repeated failed polls of an endpoint that's
# still down into one incident per window. Sources can't query existing
# incident state (see RawFailure docstring), so a stable time-bucket is the
# only dedup key available without touching the database.


def _bucket_start(moment: datetime) -> datetime:
    floored_minute = moment.minute - (moment.minute % _BUCKET_MINUTES)
    return moment.replace(minute=floored_minute, second=0, microsecond=0)


def check_api_health(client: httpx.Client | None = None) -> list[RawFailure]:
    """Probes every URL in MONITORED_ENDPOINTS right now and flags 5xx
    responses or unreachable endpoints. A live check, not a history query —
    unlike GitHub Actions there's no `since` cutoff to page through.
    """
    endpoints = settings.monitored_endpoint_list
    if not endpoints:
        return []

    owns_client = client is None
    http_client = client or httpx.Client(timeout=10.0)

    failures: list[RawFailure] = []
    try:
        for endpoint in endpoints:
            now = datetime.now(timezone.utc)
            status_code: int | None = None
            error: str | None = None
            try:
                status_code = http_client.get(endpoint).status_code
            except httpx.RequestError as exc:
                error = str(exc)

            if status_code is None or status_code >= 500:
                bucket = _bucket_start(now)
                status_label = str(status_code) if status_code else "unreachable"
                failures.append(
                    RawFailure(
                        source=IncidentSource.API_HEALTH,
                        external_id=f"{endpoint}:{bucket.isoformat()}",
                        title=f"API health check failed: {endpoint} ({status_label})",
                        detected_at=now,
                        raw_payload={
                            "endpoint": endpoint,
                            "status_code": status_code,
                            "error": error,
                        },
                    )
                )
    finally:
        if owns_client:
            http_client.close()

    return failures
