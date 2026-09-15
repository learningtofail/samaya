# Samaya

Kingshot Community Event Scheduler — self-hosted FastAPI + PostgreSQL + Discord integration.
Multi-tenant: each alliance is its own tenant with its own admin access; some
events are shared across every alliance in a Kingdom.

## Live

- MOD's calendar: https://ks138.taraka.dev/t/mod/events
- NSR's calendar: https://ks138.taraka.dev/t/nsr/events
- ICS feeds: https://ks138.taraka.dev/t/{tenant_slug}/ics/events.ics
- Admin UI: https://ks138.taraka.dev/admin (Cloudflare Access gated at the network edge, Discord OAuth login required within)

## Stack

- FastAPI + uvicorn, APScheduler, PostgreSQL 16, Docker Compose
- Caddy reverse proxy (native), Cloudflare Tunnel + Access
- Discord OAuth2 for admin login (register an app at https://discord.com/developers/applications)

## Quick Start

    cp .env.example .env
    # fill in DB_PASSWORD, SECRET_KEY, and the DISCORD_OAUTH_* values
    # from your Discord application, at minimum
    docker compose up --build -d
    curl http://127.0.0.1:8000/health

## Common Operations

    docker compose restart app
    docker compose logs app -f
    docker compose exec db psql -U taraka -d kingshot_scheduler

Manually triggering a regeneration now requires a logged-in session and a
tenant header — do this from the admin UI's Schedule tab rather than a bare
curl; the endpoint itself is `POST /admin/api/scheduler/regenerate` with
`X-Tenant-Slug: <slug>` and the session cookie a browser would already have.

## Running Tests

    pip install -r app/requirements-dev.txt
    cd app && pytest

`requirements.txt` is runtime-only (what the Docker image installs);
`requirements-dev.txt` adds pytest and an in-memory SQLite driver on top of it.

## Notes

- All times UTC
- A tenant's own Discord bot token/guild ID/public key are managed via the admin UI's Platform tab (superadmin only), not the Config tab — Config now only lists a tenant's own channels/roles for the event-creation form
- The first superadmin(s) come from `SUPERADMIN_DISCORD_IDS` in `.env` (comma-separated Discord user IDs) — there's no other way to bootstrap platform admin access
- Backup: add /opt/taraka/postgres/ to restic with pg_dump pre-hook
- Docs: Samaya Self-Hosted Architecture PRD v2.0 (Google Drive) — predates the multi-tenant/auth rework; `CLAUDE.md` is the current source of truth for repo structure
