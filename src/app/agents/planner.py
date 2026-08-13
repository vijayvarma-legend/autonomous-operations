"""Planner Agent: reads an Incident and decides which investigator agents
are actually needed for it, via an LLM call. Does not run investigators
itself — just selects and records which ones should run.
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.llm import complete
from app.models import AuditLogEntry, Incident, IncidentState

VALID_INVESTIGATORS = ["log", "git_diff", "database", "dependency", "infrastructure"]

_SYSTEM_PROMPT = f"""You are the Planner agent in an incident response system. Given a
normalized failure incident, decide which investigator agents should gather evidence.

Available investigators: {", ".join(VALID_INVESTIGATORS)}
- log: application error logs
- git_diff: recent commits/diffs that may correlate with the incident
- database: DB connectivity, slow queries, schema/migration state
- dependency: recently changed or broken third-party dependencies
- infrastructure: container/service health (Docker)

Select only the investigators relevant to this incident — not all of them by default.
Respond with ONLY a JSON object, no other text, in this exact shape:
{{"investigators": ["<name>", ...], "reasoning": "<one or two sentences>"}}"""


def plan_investigation(db: Session, incident: Incident) -> list[str]:
    """Selects investigators for `incident`, records the decision to the
    audit trail, and advances the incident to INVESTIGATING. Returns the
    selected investigator names.
    """
    user_prompt = (
        f"Incident source: {incident.source.value}\n"
        f"Title: {incident.title}\n"
        f"Raw payload: {json.dumps(incident.raw_payload)}"
    )

    raw_response = complete(_SYSTEM_PROMPT, user_prompt)
    investigators, reasoning = _parse_response(raw_response)

    incident.state = IncidentState.INVESTIGATING
    db.add(
        AuditLogEntry(
            incident_id=incident.id,
            actor="planner",
            action="investigators_selected",
            detail={"investigators": investigators, "reasoning": reasoning},
        )
    )
    db.commit()
    return investigators


def _parse_response(raw_response: str) -> tuple[list[str], str]:
    """LLM output is untrusted input — must be validated at this boundary
    before it drives which agents run next.
    """
    try:
        parsed = json.loads(raw_response)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Planner LLM did not return valid JSON: {raw_response!r}") from exc

    investigators = [i for i in parsed.get("investigators", []) if i in VALID_INVESTIGATORS]
    if not investigators:
        raise ValueError(f"Planner LLM selected no valid investigators: {raw_response!r}")

    return investigators, parsed.get("reasoning", "")
