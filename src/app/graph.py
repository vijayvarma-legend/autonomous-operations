"""LangGraph wiring for the end-to-end incident flow:

    Planner -> parallel Investigators -> Decision -> Recovery -> Verifier
    -> (retry diagnosis on failure) -> [human approval, via the API, not
    this graph]

Pure orchestration — every node is a thin wrapper around the already-
tested agent functions in app.agents.*; no business logic lives here.
State carried through the graph is just the incident id: every node
re-fetches the Incident fresh from the DB, and all real mutation happens
via the wrapped functions' own db.commit() calls, not via graph state.

Resumption: Recovery opens a PR but CI takes real wall-clock time, so a
fresh run will typically end at Verifier with a "pending" result — the
graph can't block waiting on that. Rather than add a checkpointer,
`run_incident` uses the incident's own current state to decide where to
start (see _route_entry): DETECTED starts a new run at Planner, DIAGNOSING
resumes at Decision (this is what makes the verifier-failure retry loop
real), VERIFYING resumes at Verifier (re-check CI later). Nothing yet
calls run_incident on a timer for the VERIFYING case — that scheduler is
future work, not part of this piece.
"""

from __future__ import annotations

import uuid
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from sqlalchemy.orm import Session

from app.agents.decision import decide
from app.agents.investigators.git_diff_investigator import investigate as git_diff_investigate
from app.agents.investigators.log_investigator import investigate as log_investigate
from app.agents.planner import plan_investigation
from app.agents.recovery import recover
from app.agents.verifier import verify
from app.models import AuditLogEntry, Incident, IncidentState

# Planner may select database/dependency/infrastructure — those
# investigators aren't implemented yet (their sources are still deferred,
# see CLAUDE.md), so they're silently skipped rather than failing the run.
INVESTIGATOR_NODES = {
    "log": "log_investigator",
    "git_diff": "git_diff_investigator",
}

_ENTRY_ROUTES = {
    IncidentState.DETECTED: "planner",
    IncidentState.DIAGNOSING: "decision",
    IncidentState.VERIFYING: "verifier",
}


class IncidentGraphState(TypedDict):
    incident_id: str


def _get_db(config) -> Session:
    return config["configurable"]["db"]


def _get_incident(config, state: IncidentGraphState) -> Incident:
    db = _get_db(config)
    incident = db.get(Incident, uuid.UUID(state["incident_id"]))
    if incident is None:
        raise ValueError(f"Incident {state['incident_id']} not found")
    return incident


def _planner_node(state: IncidentGraphState, config) -> dict:
    plan_investigation(_get_db(config), _get_incident(config, state))
    return {}


def _log_investigator_node(state: IncidentGraphState, config) -> dict:
    log_investigate(_get_db(config), _get_incident(config, state))
    return {}


def _git_diff_investigator_node(state: IncidentGraphState, config) -> dict:
    git_diff_investigate(_get_db(config), _get_incident(config, state))
    return {}


def _decision_node(state: IncidentGraphState, config) -> dict:
    decide(_get_db(config), _get_incident(config, state))
    return {}


def _recovery_node(state: IncidentGraphState, config) -> dict:
    recover(_get_db(config), _get_incident(config, state))
    return {}


def _verifier_node(state: IncidentGraphState, config) -> dict:
    verify(_get_db(config), _get_incident(config, state))
    return {}


def _route_entry(state: IncidentGraphState, config) -> str:
    incident = _get_incident(config, state)
    return _ENTRY_ROUTES.get(incident.state, END)


def _route_to_investigators(state: IncidentGraphState, config) -> str | list[Send]:
    db = _get_db(config)
    incident = _get_incident(config, state)
    entry = (
        db.query(AuditLogEntry)
        .filter(AuditLogEntry.incident_id == incident.id, AuditLogEntry.actor == "planner")
        .order_by(AuditLogEntry.created_at.desc())
        .first()
    )
    selected = entry.detail["investigators"] if entry else []
    nodes = [INVESTIGATOR_NODES[name] for name in selected if name in INVESTIGATOR_NODES]
    return [Send(node, state) for node in nodes] if nodes else "decision"


def _route_after_decision(state: IncidentGraphState, config) -> str:
    incident = _get_incident(config, state)
    return "recovery" if incident.state == IncidentState.FIX_PROPOSED else END


def _route_after_recovery(state: IncidentGraphState, config) -> str:
    incident = _get_incident(config, state)
    return "verifier" if incident.state == IncidentState.VERIFYING else END


def _route_after_verification(state: IncidentGraphState, config) -> str:
    incident = _get_incident(config, state)
    return "decision" if incident.state == IncidentState.DIAGNOSING else END


def _build_graph():
    graph = StateGraph(IncidentGraphState)
    graph.add_node("planner", _planner_node)
    graph.add_node("log_investigator", _log_investigator_node)
    graph.add_node("git_diff_investigator", _git_diff_investigator_node)
    graph.add_node("decision", _decision_node)
    graph.add_node("recovery", _recovery_node)
    graph.add_node("verifier", _verifier_node)

    graph.add_conditional_edges(START, _route_entry, ["planner", "decision", "verifier", END])
    graph.add_conditional_edges(
        "planner",
        _route_to_investigators,
        ["log_investigator", "git_diff_investigator", "decision"],
    )
    graph.add_edge("log_investigator", "decision")
    graph.add_edge("git_diff_investigator", "decision")
    graph.add_conditional_edges("decision", _route_after_decision, ["recovery", END])
    graph.add_conditional_edges("recovery", _route_after_recovery, ["verifier", END])
    graph.add_conditional_edges("verifier", _route_after_verification, ["decision", END])

    return graph.compile()


_COMPILED_GRAPH = _build_graph()


def run_incident(db: Session, incident_id: uuid.UUID) -> Incident:
    """Runs (or resumes) the incident state machine starting from wherever
    incident.state currently sits. The single entry point for both a
    brand-new incident and re-triggering an existing one.
    """
    _COMPILED_GRAPH.invoke(
        {"incident_id": str(incident_id)},
        config={"configurable": {"db": db}},
    )
    return db.get(Incident, incident_id)
