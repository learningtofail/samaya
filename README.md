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

### Adding a new public route

Caddy sits in front of this app on `lxc-taraka`, outside this repo, at
`/etc/caddy/Caddyfile` (native systemd service, not a container — `systemctl
status caddy`/`journalctl -xeu caddy` for its logs). It does *not* have a
catch-all proxy rule; each public path is listed individually. **Any new
top-level public route added to the app (a new page, a new unauthenticated
API prefix) needs a matching `reverse_proxy /that-path 127.0.0.1:8000` line
added there too**, or it 404s/blanks out in the browser even though the app
itself serves it fine on `127.0.0.1:8000` — this bit us once with `/feedback`
(spec §40-43) shipping in the app before Caddy knew about it.

That Caddyfile also sets `admin off`, which disables Caddy's local admin API.
That means `systemctl reload caddy` (and `caddy reload --force`) **always
fail** with `dial tcp [::1]:2019: connect: connection refused` — reload
works by POSTing the new config to that API. A config change there needs a
full restart instead:

    caddy validate --config /etc/caddy/Caddyfile   # catch syntax errors first
    systemctl restart caddy
    systemctl status caddy --no-pager

A restart briefly drops the listener (sub-second), unlike a reload — fine
for a deliberate change, just don't expect a clean zero-downtime reload here.

## Schema Migrations

Alembic (already a listed dependency, unused until now). Every future schema
change goes through it — not a new hand-written `migrate_*.py` script, which
was this repo's only option before an Alembic baseline existed.

    cd app && alembic revision --autogenerate -m "add whatever_column"
    # review the generated file under app/alembic/versions/ before committing —
    # autogenerate is a starting point, not a guarantee (it won't catch a
    # renamed column, for instance — that needs to be hand-edited into an
    # op.alter_column instead of a drop+add)
    docker compose exec app alembic upgrade head   # deploy step, after a backup

The one-time step this app's actual production database needs, since it
already has this exact schema (built up via the 10 hand-rolled `migrate_*.py`
scripts before Alembic existed): `alembic stamp head`, not `alembic upgrade
head` — see the baseline revision's own docstring
(`app/alembic/versions/6fc935931248_baseline_schema_as_of_spec_62.py`) for
why upgrading instead of stamping would fail outright (it would try to
`CREATE TABLE` on tables that already exist).

A fresh install (no existing database) runs `alembic upgrade head` normally.

## Rollback

See [`docs/rollback-runbook.md`](docs/rollback-runbook.md) — code-only
rollback, rolling back a migration (and when that's actually safe), and
restoring from `ops/backup.sh`'s nightly dump.

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
