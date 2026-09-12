# Samaya — repo map

FastAPI + PostgreSQL + Discord event scheduler for the Kingshot K138 community,
deployed at `/opt/taraka` on `lxc-taraka`, serving `ks138.taraka.dev`. This file
exists so you (Claude) can find the 1-2 files a task actually needs instead of
reading the whole tree. Update it when files are added, removed, or renamed —
not on every logic change inside a file.

## Top level

- `README.md` — live URLs, stack, quick start, common ops commands
- `docker-compose.yml` — app + Postgres 16, Caddy/Cloudflare Tunnel in front (not in this repo)
- `.env.example` — required env vars (`DB_PASSWORD`, `SECRET_KEY`, SMTP settings)
- `package.json` / `eslint.config.mjs` — lints `app/static/` JS only; no JS build step, no bundler
- `commit-step9.sh` — one-off deploy script from a past session (hardcoded commit message); not a reusable tool

## `app/main.py`

FastAPI app entrypoint. Owns: the `ADMIN_API_KEY`-required startup check, the
`lifespan` context (creates tables, seeds `SchedulerState` rows, runs a
catch-up regeneration if overdue, starts APScheduler), the validation-error
handler, and wiring all routers/static mount together. Touch this for
startup/shutdown behavior or to add a new top-level router — not for
route logic itself.

## `app/routers/` — HTTP layer

- `events.py` — public, unauthenticated: `GET /api/events`, `GET /events` (renders `static/events.html`)
- `ics.py` — `GET /ics/events.ics`, the public calendar feed
- `webhooks.py` — `POST /discord`, inbound Discord interaction webhook (signature-verified via `services/discord_api.verify_token`-adjacent logic)
- `admin/` — everything behind `X-Admin-Key` (`services/auth.require_admin_key`), mounted at `/admin`. Split by concern — **open only the file for the concern you're touching**:
  - `ui.py` — serves `static/admin.html`, nothing else
  - `status.py` — `GET /api/status`
  - `events.py` — admin event CRUD (create/list/patch/deactivate/permanent-delete)
  - `occurrences.py` — occurrence list/patch, and the two Discord-posting endpoints (`post`, `delete .../discord`)
  - `scheduler_control.py` — manual `preview` and `regenerate`
  - `post_log.py` — PostLog list + CSV export
  - `discord_config.py` — bot token/guild config, channel/role listing
  - `discord_sync.py` — the Discord↔PostLog reconciliation endpoints (`sync/discord`, `push`, `acknowledge`, `push-by-log`, `mark-cancelled`)
  - `schemas.py` — shared Pydantic request models (`EventIn`, `EventPatch`, `DiscordConfigIn`, `OccurrencePatch`)
  - `serializers.py` — shared `_event_dict`/`_occurrence_dict`/`_log_dict` response shaping
  - `deps.py` — shared FastAPI dependencies (`get_discord_config`, `get_occurrence_with_event`) and the `find_post_log` helper; used by 3+ of the files above, so check here before writing yet another inline lookup
  - `__init__.py` — just wires the above into one `router` (no logic to change here)

## `app/services/` — business logic, no HTTP

- `discord_api.py` — Discord REST client (create/update/cancel scheduled events, channels, roles, token verify, send message). One cohesive concern; not split further on purpose.
- `recurrence.py` — interval-in-days + anchor-date occurrence math (no named recurrence types — see architectural decision in project memory if you need the "why")
- `validators.py` — shared Pydantic field-validator bodies for `EventIn`/`EventPatch` (the `allow_none` flag is what differs between create vs. patch)
- `auth.py` — `require_admin_key` dependency, constant-time compare, fails closed if `ADMIN_API_KEY` unset
- `notifications.py` — SMTP email sending (separate from Discord notifications, which live in `discord_api.py`)

## `app/scheduler/jobs.py`

APScheduler job bodies: `regenerate_occurrences` (daily, rebuilds the
`Occurrence` table from `EventDefinition` + recurrence rules) and
`send_pre_event_reminders` (per-minute). Registered from `main.py`'s
`lifespan`, not from here.

## `app/models/`

- `db.py` — SQLAlchemy models: `EventDefinition`, `Occurrence`, `PostLog`,
  `DiscordConfig` (singleton row, id=1), `SchedulerState`
- `__init__.py` — engine/session factory, `get_db()` dependency, `DATABASE_URL` env lookup

## `app/static/` — admin UI, no build step, no framework

Plain HTML/CSS/JS served directly by `routers/admin/ui.py` (`admin.html`) and
`routers/events.py` (`events.html`, the public calendar page). Classic
`<script src>` tags loaded in order — not ES modules — so top-level
functions/`let`/`const` in one file are reachable from files loaded after it
in the same page; load order in `admin.html` is the dependency order.

- `admin.css` — all admin UI styles
- `admin.html` — markup + view sections only; no inline CSS or logic
- `js/theme.js` — self-contained IIFE, color palettes + `window.applyTheme`/`window.SAMAYA_THEMES`; loaded in `<head>` to avoid a flash of the wrong theme
- `js/common.js` — `api()`, `toast()`, `showView()`, admin-key cookie handling, shared constants (`CAT_COLORS`, `GANTT_PALETTE`, etc.) — load-bearing for every other `js/*.js` file below
- `js/dashboard.js`, `events.js`, `schedule.js`, `gantt.js`, `postlog.js`, `config.js`, `sync.js` — one per admin view, matching `admin.html`'s `<!-- DASHBOARD -->` / `<!-- EVENTS -->` / etc. section comments
- `js/init.js` — the two page-load calls (`loadDashboard()`, `renderThemeSwatches()`); always loaded last

## `app/tests/`

In-memory SQLite (`aiosqlite`) via `conftest.py`'s `db_session`/`client`
fixtures — never touches production Postgres. Organized by what's tested, not
by router file:

- `test_routes_public.py` — unauthenticated routes (`events.py`, `ics.py`)
- `test_routes_events.py` — admin event CRUD
- `test_post_occurrence.py` — the Discord-posting race-condition and error-path tests; monkeypatches `routers.admin.occurrences.create_discord_event` (patch target must match wherever that name is actually imported, not where it's defined in `services/discord_api.py`)
- `test_recurrence.py` — `services/recurrence.py` math, no HTTP
- `test_validation.py` — `services/validators.py` / schema validation

Run with `pip install -r app/requirements-dev.txt && cd app && pytest`.
`requirements.txt` is runtime-only (what the Docker image installs);
`requirements-dev.txt` layers pytest + aiosqlite on top for local/test use.

## Conventions worth knowing before editing

- All timestamps are UTC; `DiscordConfig` is a singleton row (`id=1`), not a table you'd expect more than one row in
- Admin routes have two independent auth layers: Cloudflare Access at the edge (not in this repo) and `X-Admin-Key` in the app itself — don't remove the latter thinking Access alone covers it
- Single uvicorn worker only — APScheduler runs in-process in `main.py`'s lifespan; a second worker would double-run the daily regeneration and per-minute reminder jobs
