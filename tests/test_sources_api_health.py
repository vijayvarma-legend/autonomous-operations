import httpx

from app.models import IncidentSource
from app.sources.api_health import _bucket_start, check_api_health


class FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class FakeClient:
    def __init__(self, responses: dict):
        self._responses = responses
        self.requested: list[str] = []

    def get(self, url: str):
        self.requested.append(url)
        result = self._responses[url]
        if isinstance(result, Exception):
            raise result
        return result


def test_flags_5xx_response(monkeypatch):
    monkeypatch.setattr(
        "app.sources.api_health.settings.monitored_endpoints", "https://api.example.com/health"
    )
    client = FakeClient({"https://api.example.com/health": FakeResponse(503)})

    result = check_api_health(client=client)

    assert len(result) == 1
    assert result[0].source == IncidentSource.API_HEALTH
    assert result[0].raw_payload["status_code"] == 503
    assert "503" in result[0].title


def test_ignores_healthy_endpoint(monkeypatch):
    monkeypatch.setattr(
        "app.sources.api_health.settings.monitored_endpoints", "https://api.example.com/health"
    )
    client = FakeClient({"https://api.example.com/health": FakeResponse(200)})

    assert check_api_health(client=client) == []


def test_flags_unreachable_endpoint(monkeypatch):
    monkeypatch.setattr(
        "app.sources.api_health.settings.monitored_endpoints", "https://api.example.com/health"
    )
    client = FakeClient(
        {"https://api.example.com/health": httpx.ConnectError("connection refused")}
    )

    result = check_api_health(client=client)

    assert len(result) == 1
    assert result[0].raw_payload["status_code"] is None
    assert "unreachable" in result[0].title


def test_returns_empty_when_no_endpoints_configured(monkeypatch):
    monkeypatch.setattr("app.sources.api_health.settings.monitored_endpoints", "")

    assert check_api_health(client=FakeClient({})) == []


def test_checks_every_configured_endpoint(monkeypatch):
    monkeypatch.setattr(
        "app.sources.api_health.settings.monitored_endpoints",
        "https://api.example.com/a, https://api.example.com/b",
    )
    client = FakeClient(
        {
            "https://api.example.com/a": FakeResponse(200),
            "https://api.example.com/b": FakeResponse(500),
        }
    )

    result = check_api_health(client=client)

    assert client.requested == ["https://api.example.com/a", "https://api.example.com/b"]
    assert [f.raw_payload["endpoint"] for f in result] == ["https://api.example.com/b"]


def test_bucket_start_floors_to_window():
    from datetime import datetime, timezone

    moment = datetime(2026, 8, 13, 10, 7, 42, tzinfo=timezone.utc)
    assert _bucket_start(moment) == datetime(2026, 8, 13, 10, 5, tzinfo=timezone.utc)
