from __future__ import annotations

from datetime import datetime

from github import Github

from app.config import settings
from app.models import IncidentSource
from app.sources import RawFailure


def fetch_failed_workflow_runs(
    since: datetime,
    branch: str | None = None,
    client: Github | None = None,
) -> list[RawFailure]:
    """Failed GitHub Actions runs on `branch` (default: the repo's default
    branch) created at or after `since`. Stateless — the caller (Watcher)
    owns dedup, this function just reports what GitHub currently shows.
    """
    if not settings.github_token:
        raise RuntimeError("GITHUB_TOKEN is not configured")
    if not settings.github_repo:
        raise RuntimeError("GITHUB_REPO is not configured")

    gh = client or Github(settings.github_token)
    repo = gh.get_repo(settings.github_repo)
    target_branch = branch or repo.default_branch

    runs = repo.get_workflow_runs(branch=target_branch, status="failure")

    failures: list[RawFailure] = []
    for run in runs:
        # Runs are returned newest-first, so once we're older than `since`
        # every remaining run is too — safe to stop instead of paging
        # through the repo's entire failure history on every poll.
        if run.created_at < since:
            break
        failures.append(
            RawFailure(
                source=IncidentSource.GITHUB_ACTIONS,
                external_id=str(run.id),
                title=f"CI failure: {run.name} on {run.head_branch}",
                detected_at=run.created_at,
                raw_payload={
                    "run_id": run.id,
                    "workflow_name": run.name,
                    "head_branch": run.head_branch,
                    "head_sha": run.head_sha,
                    "html_url": run.html_url,
                    "run_attempt": run.run_attempt,
                    "actor": run.actor.login if run.actor else None,
                },
            )
        )
    return failures
