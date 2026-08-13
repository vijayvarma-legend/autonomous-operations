"""Recovery Agent: creates a patch, rollback, config change, or restart in
an isolated branch/sandbox. Never touches main directly (see CLAUDE.md
safety rules).
"""

from __future__ import annotations

from github import Github
from sqlalchemy.orm import Session

from app.config import settings
from app.llm import complete
from app.models import AuditLogEntry, Incident, IncidentState

_DRAFT_SYSTEM_PROMPT = """You are the Recovery Agent in an incident response system. You are
given the current contents of a file implicated in an incident's root cause, along with the
diagnosis. Rewrite the ENTIRE file with a fix applied. Preserve everything not related to the
fix exactly as-is. Respond with ONLY the complete corrected file content — no markdown code
fences, no explanation, no commentary."""


def recover(db: Session, incident: Incident, client: Github | None = None) -> dict:
    """Proposes a fix for `incident` on a new branch/PR, per the Decision
    Agent's chosen strategy and the Git Diff Investigator's evidence of
    which files are implicated. Never merges or touches the base branch.
    """
    if not settings.github_token:
        raise RuntimeError("GITHUB_TOKEN is not configured")
    if not settings.github_repo:
        raise RuntimeError("GITHUB_REPO is not configured")

    attempts = _count_recovery_attempts(db, incident)
    if attempts >= settings.max_recovery_attempts:
        incident.state = IncidentState.ESCALATED
        _record(
            db,
            incident,
            "recovery_capped",
            {"attempts": attempts, "max_recovery_attempts": settings.max_recovery_attempts},
        )
        db.commit()
        return {"status": "capped"}

    decision = _latest_decision(db, incident)
    if decision is None:
        raise ValueError(
            f"No decision found for incident {incident.id}; run the Decision Agent first"
        )
    strategy = decision.detail["recovery_strategy"]

    if strategy == "restart":
        # No infrastructure execution surface exists yet (Docker/infra source
        # is still deferred) — a real restart action needs a human to run it,
        # not a branch/PR, so this can't be automated safely.
        incident.state = IncidentState.NEEDS_HUMAN_TRIAGE
        _record(
            db,
            incident,
            "recovery_skipped",
            {"reason": "restart requires infrastructure execution capability, not yet implemented"},
        )
        db.commit()
        return {"status": "skipped", "reason": "restart not automatable yet"}

    commit_evidence = _latest_git_commit(db, incident)
    if commit_evidence is None:
        raise ValueError(
            f"No git diff evidence found for incident {incident.id}; cannot locate files to fix"
        )

    gh = client or Github(settings.github_token)
    repo = gh.get_repo(settings.github_repo)
    base_branch = incident.raw_payload.get("head_branch") or repo.default_branch
    branch_name = f"auto-fix/{incident.id}-{attempts + 1}"

    base_sha = repo.get_branch(base_branch).commit.sha
    repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=base_sha)

    changed_files = [
        _apply_fix(repo, branch_name, incident, decision, strategy, commit_evidence, f["filename"])
        for f in commit_evidence["files"]
    ]

    pr = repo.create_pull(
        title=f"[Auto-Recovery] {incident.title}",
        body=_pr_body(incident, decision, changed_files),
        head=branch_name,
        base=base_branch,
    )

    incident.state = IncidentState.VERIFYING
    _record(
        db,
        incident,
        "fix_proposed",
        {
            "strategy": strategy,
            "branch": branch_name,
            "files_changed": changed_files,
            "pr_number": pr.number,
            "pr_url": pr.html_url,
        },
    )
    db.commit()
    return {"status": "proposed", "pr_url": pr.html_url, "branch": branch_name}


def _apply_fix(repo, branch_name, incident, decision, strategy, commit_evidence, path) -> str:
    current = repo.get_contents(path, ref=branch_name)

    if strategy == "rollback":
        parent_sha = repo.get_commit(commit_evidence["sha"]).parents[0].sha
        new_content = repo.get_contents(path, ref=parent_sha).decoded_content.decode("utf-8")
        message = f"Revert {path} to pre-incident state (incident {incident.id})"
    else:  # patch / config_change
        current_content = current.decoded_content.decode("utf-8")
        new_content = _draft_fix(path, current_content, decision.detail)
        message = f"Auto-fix {path} for incident {incident.id}"

    repo.update_file(path, message, new_content, current.sha, branch=branch_name)
    return path


def _draft_fix(path: str, current_content: str, diagnosis: dict) -> str:
    user_prompt = (
        f"File: {path}\n"
        f"Root cause: {diagnosis['root_cause']}\n"
        f"Reasoning: {diagnosis.get('reasoning', '')}\n\n"
        f"Current file content:\n{current_content}"
    )
    raw_response = complete(_DRAFT_SYSTEM_PROMPT, user_prompt)
    return _strip_code_fence(raw_response)


def _strip_code_fence(text: str) -> str:
    """LLM output is untrusted input — despite instructions, models
    sometimes wrap file content in ``` fences anyway. Strip them if present
    rather than committing them as literal file content.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines)
    return stripped


def _pr_body(incident: Incident, decision: AuditLogEntry, changed_files: list[str]) -> str:
    return (
        f"Auto-generated recovery for incident `{incident.id}` ({incident.source.value}).\n\n"
        f"**Root cause:** {decision.detail['root_cause']}\n"
        f"**Strategy:** {decision.detail['recovery_strategy']}\n"
        f"**Reasoning:** {decision.detail.get('reasoning', '')}\n"
        f"**Files changed:** {', '.join(changed_files)}\n\n"
        "This PR was generated automatically and has not been verified or human-approved. "
        "Do not merge without review."
    )


def _count_recovery_attempts(db: Session, incident: Incident) -> int:
    return (
        db.query(AuditLogEntry)
        .filter(
            AuditLogEntry.incident_id == incident.id,
            AuditLogEntry.actor == "recovery",
            AuditLogEntry.action == "fix_proposed",
        )
        .count()
    )


def _latest_decision(db: Session, incident: Incident) -> AuditLogEntry | None:
    return (
        db.query(AuditLogEntry)
        .filter(AuditLogEntry.incident_id == incident.id, AuditLogEntry.actor == "decision")
        .order_by(AuditLogEntry.created_at.desc())
        .first()
    )


def _latest_git_commit(db: Session, incident: Incident) -> dict | None:
    entry = (
        db.query(AuditLogEntry)
        .filter(
            AuditLogEntry.incident_id == incident.id,
            AuditLogEntry.actor == "git_diff_investigator",
        )
        .order_by(AuditLogEntry.created_at.desc())
        .first()
    )
    if entry is None or not entry.detail.get("commits"):
        return None
    return entry.detail["commits"][0]


def _record(db: Session, incident: Incident, action: str, detail: dict) -> None:
    db.add(AuditLogEntry(incident_id=incident.id, actor="recovery", action=action, detail=detail))
