from datetime import datetime, timezone

import pytest

from app.models import IncidentSource
from app.sources.app_logs import scan_error_logs

EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_flags_error_line():
    lines = ["2026-08-13 10:15:32,123 ERROR app.api.routes: Unhandled exception in /users"]

    result = scan_error_logs(EPOCH, lines=lines)

    assert len(result) == 1
    failure = result[0]
    assert failure.source == IncidentSource.APP_LOGS
    assert failure.raw_payload["level"] == "ERROR"
    assert failure.raw_payload["logger"] == "app.api.routes"
    assert failure.raw_payload["message"] == "Unhandled exception in /users"
    assert failure.detected_at == datetime(2026, 8, 13, 10, 15, 32, 123000, tzinfo=timezone.utc)


def test_flags_critical_line():
    lines = ["2026-08-13 10:15:32,123 CRITICAL app.db: connection pool exhausted"]

    result = scan_error_logs(EPOCH, lines=lines)

    assert len(result) == 1
    assert result[0].raw_payload["level"] == "CRITICAL"


def test_ignores_non_error_levels():
    lines = [
        "2026-08-13 10:15:32,123 INFO app.main: startup complete",
        "2026-08-13 10:15:33,000 WARNING app.db: slow query (1.2s)",
        "2026-08-13 10:15:34,000 DEBUG app.cache: hit ratio 0.92",
    ]

    assert scan_error_logs(EPOCH, lines=lines) == []


def test_ignores_lines_before_cutoff():
    since = datetime(2026, 8, 13, 10, 0, 0, tzinfo=timezone.utc)
    lines = [
        "2026-08-13 09:59:59,000 ERROR app.api: before cutoff",
        "2026-08-13 10:00:01,000 ERROR app.api: after cutoff",
    ]

    result = scan_error_logs(since, lines=lines)

    assert len(result) == 1
    assert result[0].raw_payload["message"] == "after cutoff"


def test_skips_unparseable_lines():
    lines = [
        "not a log line at all",
        "Traceback (most recent call last):",
        "2026-08-13 10:15:32,123 ERROR app.api.routes: real error",
    ]

    result = scan_error_logs(EPOCH, lines=lines)

    assert len(result) == 1
    assert result[0].raw_payload["message"] == "real error"


def test_same_line_produces_same_external_id():
    lines = ["2026-08-13 10:15:32,123 ERROR app.api.routes: Unhandled exception in /users"]

    first = scan_error_logs(EPOCH, lines=lines)
    second = scan_error_logs(EPOCH, lines=lines)

    assert first[0].external_id == second[0].external_id


def test_same_message_different_timestamp_produces_different_external_id():
    lines = [
        "2026-08-13 10:15:32,123 ERROR app.api.routes: Unhandled exception in /users",
        "2026-08-13 10:20:00,000 ERROR app.api.routes: Unhandled exception in /users",
    ]

    result = scan_error_logs(EPOCH, lines=lines)

    assert result[0].external_id != result[1].external_id


def test_requires_app_log_path_when_no_lines_given(monkeypatch):
    monkeypatch.setattr("app.sources.app_logs.settings.app_log_path", "")

    with pytest.raises(RuntimeError, match="APP_LOG_PATH"):
        scan_error_logs(EPOCH)
