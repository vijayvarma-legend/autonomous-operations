from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone

from app.config import settings
from app.models import IncidentSource
from app.sources import RawFailure

_ERROR_LEVELS = {"ERROR", "CRITICAL"}

# Matches Python's default logging format:
# "2026-08-13 10:15:32,123 ERROR app.api.routes: message text"
_LOG_LINE_RE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) "
    r"(?P<level>\w+) (?P<logger>[\w.]+): (?P<message>.*)$"
)


@dataclass(frozen=True)
class _ParsedLine:
    timestamp: datetime
    level: str
    logger: str
    message: str


def _parse_log_line(line: str) -> _ParsedLine | None:
    match = _LOG_LINE_RE.match(line.rstrip("\n"))
    if match is None:
        return None
    # Log timestamps carry no tz info; the app logs in UTC, so attach it
    # explicitly to make comparison against a tz-aware `since` well-defined.
    timestamp = datetime.strptime(match["timestamp"], "%Y-%m-%d %H:%M:%S,%f").replace(
        tzinfo=timezone.utc
    )
    return _ParsedLine(
        timestamp=timestamp,
        level=match["level"],
        logger=match["logger"],
        message=match["message"],
    )


def scan_error_logs(since: datetime, lines: Iterable[str] | None = None) -> list[RawFailure]:
    """ERROR/CRITICAL log lines at or after `since`. Reads APP_LOG_PATH by
    default; pass `lines` directly to scan something else (tests, a
    different file). Unparseable lines are skipped, not errors — logs are
    free text and not every line is a leveled record.
    """
    if lines is None:
        if not settings.app_log_path:
            raise RuntimeError("APP_LOG_PATH is not configured")
        with open(settings.app_log_path, encoding="utf-8") as f:
            lines = f.readlines()

    failures: list[RawFailure] = []
    for line in lines:
        parsed = _parse_log_line(line)
        if parsed is None or parsed.level not in _ERROR_LEVELS:
            continue
        if parsed.timestamp < since:
            continue

        raw_line = line.strip()
        failures.append(
            RawFailure(
                source=IncidentSource.APP_LOGS,
                # Content hash, not line number/offset — stays stable across
                # re-reads of a growing file, which is all Watcher dedup needs.
                external_id=hashlib.sha256(raw_line.encode()).hexdigest(),
                title=f"{parsed.level} in {parsed.logger}: {parsed.message[:120]}",
                detected_at=parsed.timestamp,
                raw_payload={
                    "logger": parsed.logger,
                    "level": parsed.level,
                    "message": parsed.message,
                    "raw_line": raw_line,
                },
            )
        )
    return failures
