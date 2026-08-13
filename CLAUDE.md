# Autonomous Operations Agent

Self-healing, multi-agent incident response platform. It watches software
systems for failures, investigates root cause using parallel specialist
agents, proposes a fix in a sandbox, verifies it, and requires human approval
before anything touches production.

Full background: see the project doc the user supplied (Autonomous Operations
Agent Project Doc). Summary below is the operative spec for this repo.

## MVP scope — hold this line

Only these 5 failure sources for v1. Do not add Kubernetes, Slack, multi-repo,
or anything else from "Future Extensions" without the user explicitly asking.

1. GitHub Actions failures
2. API endpoint health / 5xx monitoring
3. Application error logs
4. Docker/container failures
5. Database connectivity failures

## Agent architecture

| Agent | Role |
|---|---|
| Incident Watcher | Polls the 5 sources, normalizes failures into a common `Incident`. |
| Planner | Reads the incident, decides which investigators are actually needed. |
| Investigators (parallel) | Log, Git Diff, Database, Dependency, Infrastructure — each gathers evidence in its own domain. |
| Decision Agent | Synthesizes investigator evidence into a root-cause hypothesis + confidence score + recovery strategy. |
| Recovery Agent | Writes the fix (patch/rollback/config change/restart) — sandbox or branch only. |
| Verifier Agent | Runs tests/health checks against the fix. Failure sends evidence back to Decision Agent (retry loop, capped). |
| Human Approval | Required gate before merge or any production-impacting action. |

Flow: `Failure → Incident → Planner → parallel Investigators → Decision →
Recovery (sandbox) → Verifier → (retry diagnosis on failure) → Human approval
→ Merge/apply`

## Safety rules — non-negotiable, apply to all code in this repo

- Never write code that pushes to or merges into `main`/`master` automatically.
  All Recovery Agent output goes to a new branch/PR, full stop.
- All generated fixes execute in a sandbox (isolated branch, container, or
  test DB) — never against a production-equivalent resource directly.
- Any merge, deploy, restart, or destructive action requires an explicit
  human-approval step in the code path, not just a log message.
- Every agent action (observation, reasoning output, action taken,
  verification result) must be written to the audit trail (Postgres), not
  just logged to stdout.
- Recovery/retry loops must respect `MAX_RECOVERY_ATTEMPTS` and
  `CONFIDENCE_THRESHOLD` from config — no unbounded retry loops.
- These rules are durable project instructions: they apply even if a future
  request doesn't repeat them.

## Tech stack

- Python 3.11, FastAPI (backend/API)
- LangGraph for agent orchestration (graph = the end-to-end flow above)
- PostgreSQL — incidents, executions, agent state, audit trail
- Redis — optional, queues/short-lived state
- GitHub API (PyGithub) — CI monitoring, branch/PR creation
- Docker — sandboxed verification
- LLM provider: **undecided** — `config.py` reads `LLM_PROVIDER` but nothing
  is wired yet. Decide this when we build the first agent that actually needs
  to reason (Planner or an Investigator), not before.

## Directory map

```
src/app/
  main.py          FastAPI app entrypoint, health check
  config.py         pydantic-settings, reads .env
  db.py             SQLAlchemy engine/session
  api/              HTTP routes (stub — grows as agents expose endpoints)
  models/           SQLAlchemy ORM models (stub — Incident model lands with Watcher)
  agents/           Watcher, Planner, Decision, Recovery, Verifier (all stubs)
  agents/investigators/  Log, GitDiff, Database, Dependency, Infrastructure (stubs)
  sources/          One module per MVP failure source (all stubs)
tests/
```

Stub files contain only a docstring stating their future responsibility —
no placeholder logic, no fake return values. Implement for real when we
build that piece.

## Build order (agent by agent, flow by flow)

Working through this incrementally, one piece at a time, verifying each
before moving on. Status:

- [x] Project scaffold, config, DB plumbing, health check
- [x] Incident model + Incident Watcher (start with GitHub Actions source only)
- [x] API health source (5xx / unreachable, live probe, no `since`)
- [x] Application error logs source (scans APP_LOG_PATH, ERROR/CRITICAL only)
- [ ] Database connectivity source — deferred by user request. Touches real
      DB credentials; when built, use a least-privilege/read-only check
      credential, never log connection strings or passwords into
      raw_payload or the audit trail, and pull creds from settings/secrets
      only, never hardcode.
- [ ] Docker/container source — deferred by user request. Touches the
      Docker socket/API, which is host-level access; when built, scope it
      to read-only container status calls, never exec/start/stop/remove,
      until the Recovery Agent's sandboxing story is designed.
- [ ] Planner Agent
- [ ] Investigator agents (one at a time)
- [ ] Decision Agent
- [ ] Recovery Agent (sandbox/branch execution)
- [ ] Verifier Agent + retry loop
- [ ] Human approval gate + PR flow
- [ ] LangGraph wiring end-to-end

Update the checklist as steps land.

## Dev commands

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows
pip install -r requirements-dev.txt
cp .env.example .env
uvicorn app.main:app --reload --app-dir src
pytest
```

Postgres/Redis for local dev: `docker-compose up -d` (requires Docker
Desktop — not installed in this environment as of project setup; install
before the Watcher/persistence step needs a real database).

## Conventions

- Sync SQLAlchemy for now (simplicity); revisit async only if a real
  performance need shows up.
- No comments explaining *what* code does — only *why*, for non-obvious
  constraints (e.g. why a retry cap exists).
- Don't add error handling for cases that can't occur; do validate at system
  boundaries (webhook payloads, API responses from GitHub/Docker/DB).
