# Samaya

Kingshot Community Event Scheduler — self-hosted FastAPI + PostgreSQL + Discord integration.
Multi-tenant: each alliance is its own tenant with its own admin access; some
events are shared across every alliance in a Kingdom.

## Live

- MOD's calendar: https://ks138.taraka.dev/events/mod
- NSR's calendar: https://ks138.taraka.dev/events/nsr
- ICS feeds: https://ks138.taraka.dev/events.ics (all alliances) and https://ks138.taraka.dev/events/{slug}.ics
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

Occurrence generation (daily, UTC 00:00, and once at startup) and the
per-minute delivery tick run in-process. If something looks stuck, open the
admin console's Delivery log tab: it shows every Discord post, why a failed
one failed, and has a Retry button. `GET /admin/api/delivery-health` reports
whether the tick is keeping up.

### Adding a new public route

Caddy sits in front of this app on `lxc-taraka`, outside this repo, at
`/etc/caddy/Caddyfile` (native systemd service, not a container — `systemctl
status caddy`/`journalctl -xeu caddy` for its logs). It does *not* have a
catch-all proxy rule; each public path is listed individually. **Any new
top-level public route added to the app (a new page, a new unauthenticated
API prefix) needs a matching `reverse_proxy /that-path 127.0.0.1:8000` line
added there too**, or it 404s/blanks out in the browser even though the app
itself serves it fine on `127.0.0.1:8000` — this bit us with `/feedback`
(spec §40-43) and again on 2026-10-02 with `/events.ics`, `/events/*`,
`/theme/*` and `/theme-assets/*`.

**An unlisted path does not 404.** Caddy answers it itself with an empty
`200`, `Content-Length: 0` and the `X-Robots-Tag` header, so the app never
sees the request and its logs show nothing. Calendar clients then report a
feed that "works" but has no events. Test a new route through the public
hostname, not only on `127.0.0.1:8000`:

    curl -s -o /dev/null -w '%{http_code} %{size_download}B %{content_type}\n' https://ks138.taraka.dev/<new-path>

A size of `0B` means the Caddyfile is missing the route.

The routes listed today: `/health`, `/webhooks/*`, `/auth/*`, `/invite/*`,
`/invite-invalid`, `/events`, `/events.ics`, `/events/*`, `/feedback`,
`/theme/*`, `/theme-assets/*`, `/api/*`, `/admin`, `/admin/*`, `/static/*`;
`/` redirects to `/admin/`. The cloudflared ingress on Vinayaki is a plain
route to `192.168.2.112:80` with no path rules, so only this file needs
changing.

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

## Cutover (unified event model)

[`docs/cutover-runbook.md`](docs/cutover-runbook.md) is the one-time,
destructive procedure for moving production from the old event and
announcement system to the unified event model.

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
- Discord bot tokens, guild IDs and each alliance's notification channel and role are managed in the admin console's Setup tab (bot credentials: superadmin only; the notification destination: alliance owner or superadmin)
- The first superadmin(s) come from `SUPERADMIN_DISCORD_IDS` in `.env` (comma-separated Discord user IDs) — there's no other way to bootstrap platform admin access
- Backup: add /opt/taraka/postgres/ to restic with pg_dump pre-hook
- Docs: Samaya Self-Hosted Architecture PRD v2.0 (Google Drive) — predates the multi-tenant/auth rework; `CLAUDE.md` is the current source of truth for repo structure
