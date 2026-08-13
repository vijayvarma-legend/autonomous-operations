"""Decision Agent: synthesizes investigator evidence into a root-cause
hypothesis, a confidence score, and a chosen recovery strategy.
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.config import settings
from app.llm import complete
from app.models import AuditLogEntry, Incident, IncidentState

VALID_STRATEGIES = ["patch", "rollback", "config_change", "restart"]

_SYSTEM_PROMPT = f"""You are the Decision Agent in an incident response system. Given an
incident and the evidence gathered by investigator agents, synthesize a root-cause
hypothesis, a confidence score, and a recovery strategy.

Recovery strategies: {", ".join(VALID_STRATEGIES)}
- patch: a code change fixes the root cause
- rollback: revert to a prior known-good commit/deploy
- config_change: the fix is a configuration value, not code
- restart: a transient/stateful failure that a restart clears

Be conservative with confidence — only score high when the evidence clearly points to
one cause. If evidence is thin, contradictory, or absent, say so and score low.

Respond with ONLY a JSON object, no other text, in this exact shape:
{{"root_cause": "<hypothesis>", "confidence": <float 0.0-1.0>,
"recovery_strategy": "<one of the above>", "reasoning": "<one or two sentences>"}}"""


def decide(db: Session, incident: Incident) -> dict:
    """Synthesizes all evidence gathered so far for `incident` into a
    root-cause hypothesis, records it to the audit trail, and routes the
    incident to FIX_PROPOSED (confidence >= CONFIDENCE_THRESHOLD) or
    NEEDS_HUMAN_TRIAGE (below threshold — CLAUDE.md safety rule: low-
    confidence diagnoses never proceed to automated recovery). Returns the
    parsed decision.
    """
    evidence = (
        db.query(AuditLogEntry)
        .filter(
            AuditLogEntry.incident_id == incident.id,
            AuditLogEntry.action == "evidence_gathered",
        )
        .order_by(AuditLogEntry.created_at)
        .all()
    )

    user_prompt = (
        f"Incident source: {incident.source.value}\n"
        f"Title: {incident.title}\n"
        f"Raw payload: {json.dumps(incident.raw_payload)}\n\n"
        f"Evidence gathered ({len(evidence)} investigator report(s)):\n"
        + (
            "\n".join(f"- {e.actor}: {json.dumps(e.detail)}" for e in evidence)
            if evidence
            else "(none — no investigator evidence was gathered)"
        )
    )

    raw_response = complete(_SYSTEM_PROMPT, user_prompt)
    decision = _parse_response(raw_response)

    incident.state = (
        IncidentState.FIX_PROPOSED
        if decision["confidence"] >= settings.confidence_threshold
        else IncidentState.NEEDS_HUMAN_TRIAGE
    )
    db.add(
        AuditLogEntry(
            incident_id=incident.id,
            actor="decision",
            action="diagnosis_completed",
            detail={**decision, "confidence_threshold": settings.confidence_threshold},
        )
    )
    db.commit()
    return decision


def _parse_response(raw_response: str) -> dict:
    """LLM output is untrusted input — must be validated at this boundary
    before it drives the incident's state and (eventually) the Recovery
    Agent's chosen strategy.
    """
    try:
        parsed = json.loads(raw_response)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Decision LLM did not return valid JSON: {raw_response!r}") from exc

    root_cause = parsed.get("root_cause")
    if not isinstance(root_cause, str) or not root_cause.strip():
        raise ValueError(f"Decision LLM returned no root_cause: {raw_response!r}")

    confidence = parsed.get("confidence")
    if not isinstance(confidence, int | float) or not 0.0 <= confidence <= 1.0:
        raise ValueError(f"Decision LLM returned an invalid confidence: {raw_response!r}")

    strategy = parsed.get("recovery_strategy")
    if strategy not in VALID_STRATEGIES:
        raise ValueError(f"Decision LLM returned an invalid recovery_strategy: {raw_response!r}")

    return {
        "root_cause": root_cause,
        "confidence": float(confidence),
        "recovery_strategy": strategy,
        "reasoning": parsed.get("reasoning", ""),
    }
