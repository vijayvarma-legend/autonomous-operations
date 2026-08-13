"""Git Diff Investigator: checks recent commits/diffs for changes that
correlate with an incident's onset. Deterministic evidence gathering — no
LLM call here; that's the Decision Agent's job once all investigators have
reported in.
"""

from __future__ import annotations

from datetime import timedelta

from github import Github
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AuditLogEntry, Incident

# Deploy-to-incident lag can be hours (issue only surfaces under real
# traffic), so this window is much wider than the Log Investigator's —
# commits, unlike log lines, are sparse enough that a wide window doesn't
# drown the signal.
WINDOW_BEFORE = timedelta(hours=24)


def investigate(db: Session, incident: Incident, client: Github | None = None) -> list[dict]:
    """Collects commits on the incident's branch (or the repo default) in
    the 24h before incident.detected_at, records them as evidence on the
    audit trail, and returns them for the Decision Agent to consume.
    """
    if not settings.github_token:
        raise RuntimeError("GITHUB_TOKEN is not configured")
    if not settings.github_repo:
        raise RuntimeError("GITHUB_REPO is not configured")

    gh = client or Github(settings.github_token)
    repo = gh.get_repo(settings.github_repo)
    branch = incident.raw_payload.get("head_branch") or repo.default_branch

    window_start = incident.detected_at - WINDOW_BEFORE
    commits = repo.get_commits(sha=branch, since=window_start, until=incident.detected_at)

    entries = [
        {
            "sha": commit.sha,
            "author": commit.commit.author.name if commit.commit.author else None,
            "date": commit.commit.author.date.isoformat() if commit.commit.author else None,
            "message": commit.commit.message,
            "html_url": commit.html_url,
            "files": [
                {
                    "filename": f.filename,
                    "status": f.status,
                    "additions": f.additions,
                    "deletions": f.deletions,
                }
                for f in commit.files
            ],
        }
        for commit in commits
    ]

    db.add(
        AuditLogEntry(
            incident_id=incident.id,
            actor="git_diff_investigator",
            action="evidence_gathered",
            detail={
                "branch": branch,
                "window_start": window_start.isoformat(),
                "window_end": incident.detected_at.isoformat(),
                "commits": entries,
            },
        )
    )
    db.commit()
    return entries
