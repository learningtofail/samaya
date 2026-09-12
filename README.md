# Samaya

Kingshot Community Event Scheduler — self-hosted FastAPI + PostgreSQL + Discord integration.

## Live

- Community calendar: https://ks138.taraka.dev/events
- ICS feed: https://ks138.taraka.dev/ics/events.ics
- Admin UI: https://ks138.taraka.dev/admin (Cloudflare Access gated)

## Stack

- FastAPI + uvicorn, APScheduler, PostgreSQL 16, Docker Compose
- Caddy reverse proxy (native), Cloudflare Tunnel + Access

## Quick Start

    cp .env.example .env
    docker compose up --build -d
    curl http://127.0.0.1:8000/health

## Common Operations

    docker compose restart app
    docker compose logs app -f
    curl -X POST http://127.0.0.1:8000/admin/api/scheduler/regenerate
    docker compose exec db psql -U taraka -d kingshot_scheduler

## Running Tests

    pip install -r app/requirements-dev.txt
    cd app && pytest

`requirements.txt` is runtime-only (what the Docker image installs);
`requirements-dev.txt` adds pytest and an in-memory SQLite driver on top of it.

## Notes

- All times UTC
- Discord secrets managed via admin UI Config tab
- Backup: add /opt/taraka/postgres/ to restic with pg_dump pre-hook
- Docs: Samaya Self-Hosted Architecture PRD v2.0 (Google Drive)
