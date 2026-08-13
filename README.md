# Autonomous Operations Agent

Self-healing, multi-agent incident response platform. See [CLAUDE.md](CLAUDE.md)
for the full architecture, safety rules, and build order.

## Setup

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows
pip install -r requirements-dev.txt
cp .env.example .env
```

Local Postgres + Redis (requires Docker Desktop):

```bash
docker-compose up -d
```

## Run

```bash
uvicorn app.main:app --reload --app-dir src
```

## Test

```bash
pytest
```
