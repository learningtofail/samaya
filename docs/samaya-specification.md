# Samaya — Technical Specification

**Repository:** github.com/learningtofail/samaya
**Version:** 2.0.0 (unified event model) · **Deployment:** `ks138.taraka.dev` (LXC `lxc-taraka`, `/opt/taraka`)
**Last reconciled with the code:** 2026-10-02, `master`, deployed to production (the unified event model, §67, §68 and the §69 refinements). Part I below is the authoritative current state. Part II keeps the original section-by-section record; where Part II and Part I differ, Part I wins, and sections replaced by §66 carry a note saying so.

# Part I: Current State

This part describes Samaya as it exists now. It is rewritten when the system changes, so it is the place to start. The numbered sections in Part II explain why each piece was built and are not edited to track later changes, except for short corrections.

**Destinations and Audiences (§67, §68).** Where an event posts is no longer a per-alliance Notifications channel with per-event overrides. The Kingdom owns Audiences, each a named list of destinations (server, channel, optional role); alliances use Audiences through links; events select several; audience groups are folded into Audiences. Where §67 below says destination or group, §68 wins. Also: reminders to the same guild, channel and offset merge into one message, and a server belongs to one Kingdom. Wherever Part I or Part II mention `notification_channel_id`, per-event channel overrides or `PUT /api/notification-destination`, §67 wins; the old columns stay until revision `a1f0c0de0006`.

## CS.1 What the system is

A self-hosted, multi-tenant event scheduler for the Kingshot community on Kingdom 138. Each alliance is a tenant. Coordinators define **events** (a calendar event with a duration, or a plain message with none), each with one **event type** that sets its label and color. The delivery engine generates occurrences, creates Discord Scheduled Events for events with a duration, and sends reminders and messages to each alliance's notification channel. Public pages and ICS feeds show what is public, and a public feedback board takes requests and error reports. §66 is the design record.

## CS.2 Stack and process model

| Layer | Current state |
|---|---|
| API | FastAPI on Uvicorn, one worker only (APScheduler runs in the web process; a second worker duplicates every job) |
| Data | SQLAlchemy 2.0 async, asyncpg, PostgreSQL 16 |
| Schema changes | Alembic only (`app/alembic/`). Revisions: baseline `6fc935931248`, `a1f0c0de0001` (event model tables), `a1f0c0de0002` (feedback moderation), `a1f0c0de0003` (drops the replaced tables; destructive, no downgrade). Startup does not create tables; `alembic upgrade head` is a deploy step |
| Scheduling | APScheduler, in process, UTC: a daily generation pass and a per-minute delivery tick |
| Frontend | Static HTML, CSS and JS, no build step and no framework. Classic script tags, not ES modules |
| Auth | Discord OAuth2 with signed session cookies; `SameSite=Strict` is the CSRF defense |
| Tooling | Ruff (Python), ESLint (public page scripts), pytest (SQLite), vitest (public page pure functions) |

## CS.3 Tenancy and data model

- **Kingdom**: a Kingshot game server, with optional `public_site_title` and `admin_console_title` (§38.7).
- **Tenant**: an alliance, with `color`, an optional `icon_image_data`, and its notification destination (`notification_channel_id`, `notification_role_id`). It points at a **DiscordServer**, which holds `guild_id`, `bot_token` and `public_key`. Several tenants may share one DiscordServer, and a shared guild gets exactly one Discord Scheduled Event per occurrence (§52).
- **Events** (§66.1): `event_types` (label, color, defaults per Kingdom), `events` (scope `alliance` or `kingdom-wide`, optional duration, recurrence `interval_days` or `none`, optional `until_date`, message, `leadership_only`, `cover_image_data`), `event_alliances` (the audience, with per-alliance message and destination overrides), `event_reminders` (minutes before start), `event_occurrences` (generated, with per-occurrence cancel, move and message override), `deliveries` (one row per Discord post: kind `discord_event` or `reminder`, status `pending`, `sending`, `posted`, `error`, `cancelled`).
- **Other tables**: `kingdoms`, `tenants`, `discord_servers`, `users` (with `display_name`), `user_tenants`, `user_kingdoms`, `invites`, `audit_log`, `tickets`, `ticket_votes`, `ticket_responses`.
- All timestamps are UTC. Use `services/time_utils.ensure_utc()` when reading them, because asyncpg and aiosqlite differ on `tzinfo`.

## CS.4 Roles and access

- Access is by invite only. Discord OAuth is the only authentication and the only way a user is created. There is no self-service signup.
- Backend roles: `owner`, `coordinator`, `viewer` per tenant (`user_tenants`), a separate kingdom-coordinator grant (`user_kingdoms`, superadmin-issued, not implied by owning an alliance; required for event types and kingdom-wide events), and the superadmin flag, bootstrapped only from `SUPERADMIN_DISCORD_IDS`.
- A `viewer` can read everything a coordinator can but every mutating call returns 403 (`require_not_viewer`).
- The admin UI shows two tiers: superadmin, and everyone else. Setup's access, platform and user sections and the Audit log tab are superadmin-only in the UI (§38.6).
- Coordinators can set their own `display_name`, which is how they appear on public feedback responses ("Team" when unset).

## CS.5 API surface

Public, unauthenticated:
- Pages: `/events`, `/events/{slug}`, `/feedback`.
- Data: `/api/events`, `/api/events/{slug}`, `/api/alliances`, `/api/kingdom-branding`, `/api/last-activity`, `/api/last-activity/{slug}`, `GET/POST /api/tickets` (`GET` returns `{active, archived}`), `POST /api/tickets/{id}/vote`. All read through `services/public_events.public_rows`, the one place that excludes inactive and leadership-only events (§66.6).
- Calendars: `/events.ics` and `/events/{slug}.ics`.
- Other: `/health`, `POST /webhooks/discord` (signature verified).
- The three ticket endpoints are rate limited per client IP (§65).

Auth: `/auth/login`, `/auth/discord/callback`, `/invite/{token}`, `POST /auth/logout`, and two static failure pages.

Admin (`/admin`, session required, tenant chosen by the `X-Tenant-Slug` header, `*` for combined read-only lists):
- Events: `event-types`, `events` (CRUD, `POST events/{id}/split`), `occurrences` (list, `PATCH`), `deliveries` (list, `POST .../retry`), `delivery-health`.
- Feedback: `tickets` (list, `PATCH`, `DELETE`), `tickets/{id}/responses`.
- Setup: kingdoms, tenants, Discord servers, `notification-destination`, invites, members, kingdom coordinators, users, `me`, `discord/channels` and `discord/roles`, `audit-log`.
The authoritative route list is the router files listed in `CLAUDE.md`.

## CS.6 Background jobs

| Job | Schedule | Purpose |
|---|---|---|
| `generation_job` | Daily 00:00 UTC, and once at startup | Idempotently create and update occurrences and their deliveries for every event, 28 days ahead; never touches cancelled or moved occurrences |
| `delivery_tick_job` | Every minute | Claim each due delivery atomically, commit, then call Discord. At most once per delivery; one failure never blocks another; a `sending` row older than 10 minutes becomes an error and is never retried automatically |

A Discord Scheduled Event is due 7 days before start and is skipped for leadership events and events with no duration. Reminders are due `minutes_before` before start. Same-name, same-time events already in Discord are recorded without a second post; a same-name, different-time event is flagged, never overwritten (§51). All logic is in `services/event_engine.py`, tested with a fake Discord client.

## CS.7 Frontend

- **Admin console** (`admin.html`, `admin.css`, `js/*.js`): PatternFly, one script per view. Tabs: Events, Schedule (table or timeline), Delivery log (Upcoming and Past tables, §69.3), Event types, Feedback, Setup, Audit log (§66.7). The message composer (toolbar, live preview, emoji picker) is `js/composer.js`. Each view has its own alliance filter kept in `localStorage`. No inline styles or inline event handlers in the markup (§65). The `js/*.js` files are not covered by ESLint.
- **Public events page** (`events.html`, `events.css`, `events-public.js`): standalone light theme with a gold accent, no PatternFly. One IIFE with no globals. Alliance filter chips, a live-now hero with countdown, schedule grouped by day, list and calendar views, 24-hour time, and a time zone choice kept under `samaya_display_tz` (§62). Pure functions are unit tested through the `__SAMAYA_TEST__` hook.
- On the public pages a Kingdom-wide event shows a neutral "Kingdom" badge, never its anchor alliance (§69.2).
- **Feedback board** (`feedback.html`, `.css`, `.js`): active tickets, a collapsed Archived section, public team responses (§66.10).
- Cloudflare caches `/static/*` by extension, so every static change bumps `STATIC_ASSET_VERSION` in `services/static_assets.py`.

## CS.8 Configuration, deployment and operations

- Variables: `DB_PASSWORD`, `SECRET_KEY`, `DISCORD_OAUTH_CLIENT_ID`, `_SECRET`, `_REDIRECT_URI`, `SUPERADMIN_DISCORD_IDS`, `PLATFORM_BOT_TOKEN`, `PLATFORM_PUBLIC_KEY`, and the SMTP settings. The app refuses to boot without `SECRET_KEY` and the OAuth client ID and secret. The earlier `SAMAYA_UNIFIED_ENGINE` flag no longer exists.
- Docker Compose runs `app` (127.0.0.1:8000) and `db` (postgres:16-alpine). Caddy runs natively on `lxc-taraka`, outside the repository and outside Compose, in front of the app. Its config is `/etc/caddy/Caddyfile` with `admin off`, so a change needs `systemctl restart caddy`, and each new public route needs a matching `reverse_proxy` line there. Cloudflare Tunnel and Access sit in front of that, and Access gates `/admin`.
- Deploy: apply the patch locally, commit, push, then on `lxc-taraka` pull, run `alembic upgrade head` if a migration is included, and restart. The one-time move to the unified event model follows `docs/cutover-runbook.md`.
- Backups: `ops/backup.sh` takes a nightly gzipped `pg_dump` with 14 day rotation (cron on the LXC host), and `restic` covers `/opt/taraka/postgres/`.
- Recovery: `docs/rollback-runbook.md` covers code rollback, Alembic downgrade (and when it cannot be trusted), restore from a dump, and the cutover rollback.

## CS.9 Testing and CI

- pytest runs against in-memory SQLite and never touches production. The suite is organized by what is tested; see `CLAUDE.md` for the file list.
- vitest covers the public events page's pure functions.
- CI (`.github/workflows/ci.yml`, every push and pull request to `master` and to `unified-event-model`): pytest after `ruff check`, a frontend job (ESLint and vitest), a Docker build that boots the image against a real Postgres and checks `/api/kingdom-branding`, and an `alembic upgrade head` plus `alembic check` job that fails when a model change has no migration.
- `ops/dev_smoke_engine.py` runs the delivery engine against a real Discord test server (dev bot and database only, never production credentials).

## CS.10 Conventions

- UTC everywhere; normalize with `ensure_utc()`.
- Admin routes scope by `X-Tenant-Slug`; public routes scope by the URL path (`/events/{slug}`), because calendar apps cannot send headers.
- Reuse `deps.py` checks and `services/db_errors.py` instead of writing inline lookups or substring-matching driver errors.
- Update `CLAUDE.md` when files are added, removed or renamed, and this spec first for any functional change.

## CS.11 Known issues and open work

- **Not built, by decision (§66.9):** monthly recurrence, leadership-only Discord channels, per-coordinator alliance limits, a creation-notice destination, downstream systems. §63.3 themes and §64 scheduled theme resolution are superseded by the §71 design (not built).
- **Admin API gaps found while building the console (§66.7):** `/api/me` does not expose kingdom-coordinator grants, so the UI cannot hide event-type and kingdom-wide controls from users who lack them (the server returns 403); event list responses carry `cover_image_data` inline; the delivery list sorts by due time, so an old error row can fall behind the page limit (the UI asks for 200 and filters); a non-superadmin editing an event whose audience includes alliances they cannot see would drop those alliances.
- **Known gaps:** no per-tenant custom Discord Interactions endpoint (§22); anonymous votes are per browser, not per person; the public page has no analytics; admin `js/*.js` has no lint coverage; Google Fonts load from an external host unless self-hosted.
- **Roadmap ideas** are collected in §44 and §66.9.

## CS.12 Section map

| Group | Sections |
|---|---|
| Original baseline, corrected where facts changed | 1 to 12 (4, 5, 6 and 12 describe the previous system; see CS.3, CS.5, CS.6, CS.9) |
| Current and still describing the system | 15, 17 (rationale only), 19 (rationale only), 21 to 26, 28, 29, 31 (audit log, viewer role), 33 to 35, 38, 40 to 46, 52, 53, 55 to 57, 59 to 62, 65, 66 |
| Replaced by §66 (kept as design history) | 13, 20, 27, 30.1, 32, 37, 45, 49, 50, 51 (semantics kept) |
| Designed, not built | 71 (supersedes the deferred parts of 63 and 64; 63.1 and 63.2 are built, 64.1 is reused), 72 |
| Archived (fully superseded) | 14, 18, 36, 39, 47, 54, 58 |
| Intentionally empty | 16, 48 |

# Part II: Specification Sections

The sections below are the original build-up record, kept for the reasoning behind each decision. Sections that were fully replaced are in the Archive at the end. Section numbers never change, so cross-references still resolve.

## 1. Purpose and Scope

Samaya is a self-hosted, multi-tenant event scheduler for the Kingshot game community (Kingdom 138). Each in-game alliance is a tenant with its own admin access and its own Discord guild; some events are scoped to a single alliance while others fan out across every alliance in a Kingdom. The system generates recurring event occurrences, posts them as Discord Scheduled Events, sends pre-event reminders, publishes public ICS calendar feeds, and delivers ad hoc scheduled text announcements.

## 2. Architecture

### 2.1 Stack

| Layer | Technology |
|---|---|
| API | FastAPI + Uvicorn (single worker) |
| ORM | SQLAlchemy 2.0 (async), asyncpg driver |
| Database | PostgreSQL 16 |
| Scheduling | APScheduler, in-process, UTC |
| Frontend | Static HTML/CSS/JS, no build step, no framework, script tags loaded in dependency order |
| Auth | Discord OAuth2, signed session cookies (`itsdangerous`) |
| Deployment | Docker Compose (app + db); Caddy reverse proxy and Cloudflare Tunnel/Access sit in front, outside this repo |
| Calendar export | `icalendar` (ICS feeds) |
| Discord signature verification | `pynacl` |
| Migrations | Alembic (`app/alembic/`) is the only convention for schema changes, from the baseline revision `6fc935931248` onward (§65). The ten earlier hand-written `migrate_*.py` scripts are historical and never run again |

### 2.2 Process model

A single Uvicorn worker is mandatory. APScheduler runs in-process inside the FastAPI lifespan; a second worker would double-run the daily regeneration job and both per-minute jobs.

### 2.3 Startup behavior (`app/main.py`)

The app refuses to boot if `SECRET_KEY`, `DISCORD_OAUTH_CLIENT_ID`, or `DISCORD_OAUTH_CLIENT_SECRET` are unset (fail-closed, replacing a prior shared `X-Admin-Key` scheme). On startup the lifespan handler no longer creates tables (schema is Alembic's job, run as a deploy step, §65). It checks, for every tenant, whether occurrence regeneration is overdue (no state row, or last run >25 hours ago) and runs a catch-up regeneration across all tenants if so. It then registers four APScheduler jobs and starts the scheduler.

## 3. Multi-Tenancy Model

- **Kingdom**: a Kingshot game server (e.g. "Kingdom 138"), distinct from a Discord server. Holds multiple Tenants.
- **Tenant**: an alliance. Each has its own admin access and references a `DiscordServer` (§25), which carries the guild ID, bot token and public key; the bot token falls back to the platform-wide bot token if unset. Several tenants can share one `DiscordServer`.
- **Scope**: every `EventDefinition` is either `alliance` (single tenant) or `kingdom-wide` (fans out to every tenant in the owning tenant's Kingdom). A kingdom-wide event has exactly one `Occurrence` row but produces one independent `PostLog` row per tenant when posted — each tenant's Discord post succeeds, fails, or is cancelled independently.
- Being an `owner` of one alliance does **not** grant kingdom-wide event rights. That is a separate `UserKingdom` grant, issued only by a superadmin.

### 3.1 Tenant scoping conventions

Public routes scope by URL path segment (`/events/{tenant_slug}`, §74) because calendar apps consuming ICS feeds cannot send custom headers. Admin API routes scope by request header (`X-Tenant-Slug`) instead — a bridge-period choice from before real per-user access checks existed, kept so route paths didn't need to change again.

## 4. Data Model

**Superseded by the unified event model.** Replaced for events and announcements by §66.1; see CS.3 for the current tables.

| Table | Purpose |
|---|---|
| `kingdoms` | Game server; `name`, unique `slug` |
| `tenants` | Alliance; `kingdom_id`, `server_id` (FK to `discord_servers`), `slug`, `color`, optional `icon_image_data` (§38.7). Guild and bot credentials live on `discord_servers`, not here (§25) |
| `discord_servers` | A Discord guild with its `guild_id`, `bot_token` and `public_key`; shared by every tenant that points at it (§25) |
| `event_targets` | Explicit extra notification destinations for an event, independent of Kingdom membership (§20) |
| `announcement_templates` | Reusable named starting points for announcements (§27) |
| `tickets`, `ticket_votes` | The public feedback, request and error-report board and its anonymous upvotes (§40 to §43) |
| `users` | Discord-identified person; created only on first OAuth login, never on invite creation; `is_superadmin` flag |
| `user_tenants` | Per-user, per-tenant grant; `role` ∈ {`owner`, `coordinator`, `viewer`} — see §31 for `viewer` |
| `user_kingdoms` | Per-user, per-kingdom grant of kingdom-wide event rights (flag only, no sub-tiers) |
| `invites` | The only path to creating a `user_tenants`/`user_kingdoms` grant, and the only way a first-time visitor completes OAuth at all; exactly one of `tenant_id`/`kingdom_id` set per `role` ∈ {`owner`, `coordinator`, `viewer`, `kingdom_coordinator`}; supports expiry and revocation |
| `audit_log` | Append-only; `action` ∈ {`create`, `update`, `delete`}; `before`/`after` stored as JSON text, not ORM references, so schema changes don't break old rows read back later. Explicitly not designed for rollback — many logged actions also reach Discord, and restoring a DB snapshot doesn't undo that |
| `event_definitions` | The recurring event template: `owning_tenant_id`, `scope`, `interval_days`, `start_time_utc`, `duration_hours`, `anchor_date`, Discord channel/notification fields, `leadership_only`, `active` |
| `event_tenant_notifications` | Per-tenant notification-channel/role override for a kingdom-wide event; a tenant with no row here still receives the Discord Scheduled Event, just no channel ping |
| `occurrences` | Generated instances of an `event_definitions` row; `post_status`, `reminder_sent`; unique per `(event_id, occurrence_date)` |
| `post_log` | One row per (tenant, occurrence) posted or attempted to Discord; unique per `(tenant_id, event_name, occurrence_date)` |
| `announcements` | Ad hoc scheduled markdown text, not built on the event/occurrence model since the shape differs (no start/end); `status` ∈ {`draft`, `scheduled`, `posted`, `failed`, `cancelled`}; `leadership_only` (categorization/display flag, mirrors `event_definitions`); `recurring` + `interval_days` (nullable, only meaningful when `recurring` is true) — see §13.5 |
| `announcement_targets` | One row per (announcement, tenant); independent post per target guild/bot; `post_status` ∈ {`pending`, `posted`, `error`} |
| `scheduler_state` | One row per (tenant, job_name) tracking last run/result/next run, so one tenant's regeneration failure doesn't mask another's |

Every timestamp is UTC. `services/time_utils.ensure_utc()` normalizes `tzinfo` because asyncpg and aiosqlite round-trip `DateTime(timezone=True)` columns differently — a real cross-driver bug that surfaced independently in three files before being consolidated.

## 5. API Surface

**Superseded by the unified event model.** Admin event, occurrence and announcement routes were replaced by §66; see CS.5.

### 5.1 Public (unauthenticated, path-scoped)

- `GET /api/events/{tenant_slug}`
- `GET /events/{tenant_slug}` — renders `static/events.html`
- `GET /events/{tenant_slug}.ics` — public ICS feed
- `GET /health`
- `GET /events`, `GET /api/events`, `GET /events.ics` — the combined, all-alliance versions of the three per-tenant routes above
- `GET /api/alliances`, `GET /api/kingdom-branding`, `GET /api/last-activity` (and `/api/last-activity/{tenant_slug}`) — public page support
- `GET /feedback`, `GET/POST /api/tickets`, `POST /api/tickets/{id}/vote` — the public feedback board (§40 to §43)

### 5.2 Auth

- `GET /invite/{token}` — invite claim
- Discord OAuth login and callback (the only path to real access)
- Logout
- `/invite-invalid`, `/auth/login-failed` — static failure pages

### 5.3 Inbound Discord

- `POST /webhooks/discord` — Discord interaction webhook, signature-verified against `PLATFORM_PUBLIC_KEY`. Answers PING and the `/schedule`, `/next` and `/feedback` slash commands (§70). **Known gap:** a tenant with its own custom bot and its own Discord "Interactions Endpoint URL" is not supported by this endpoint.

### 5.4 Admin (`/admin`, session-authenticated via `get_current_user`/`get_current_tenant`)

| Router | Responsibility |
|---|---|
| `ui.py` | Serves `static/admin.html`; deliberately has no auth dependency |
| `me.py` | `GET /api/me` — identity, tenant roles, superadmin flag; drives frontend tab visibility |
| `status.py` | `GET /api/status`; `GET /api/delivery-health` (§31.2) |
| `events.py` | Event CRUD; `PUT /api/events/{id}/notification-override` for a non-owning tenant's own ping channel on a kingdom-wide event; `GET /api/events/export.csv` / `POST /api/events/import.csv` for bulk alliance-scope events (§32.1) |
| `occurrences.py` | Occurrence list/patch; Discord post/delete endpoints; kingdom-wide fan-out logic (`_post_to_one_tenant`) |
| `scheduler_control.py` | Manual `preview` and per-tenant `regenerate` |
| `post_log.py` | PostLog list and CSV export, tenant-scoped |
| `discord_config.py` | Per-tenant channel/role listing (bot token/guild config now lives in `tenants.py`) |
| `discord_sync.py` | Discord ↔ PostLog reconciliation |
| `tenants.py` | Kingdom/Tenant CRUD — the "onboard a new alliance" surface; create/update is superadmin-only |
| `invites.py` | Tenant invites (owner-issued) and kingdom-coordinator invites (superadmin-issued), with revocation |
| `announcements.py` | Scheduled-announcement create/list/cancel/retry-failed-targets (§30.1; delivery logic lives in `scheduler/announcements.py`) |
| `audit_log.py` | `GET /api/audit-log`, owner-only (§31.1) |

Manually triggering a regeneration requires a logged-in session and an `X-Tenant-Slug` header — done from the admin UI's Schedule tab, not a bare `curl`. The endpoint is `POST /admin/api/scheduler/regenerate`.

## 6. Background Jobs (`app/scheduler/`)

**Superseded by the unified event model.** Replaced by the delivery engine (§66.4); see CS.6. The four jobs described here no longer exist.

Four independent jobs, each in its own file (split from an earlier monolithic `jobs.py`). Every public function accepts an optional `session_factory` (default `models.AsyncSessionLocal`) since jobs run outside any HTTP request and can't use FastAPI's `get_db` dependency — tests must inject a test session factory or the job will hit the real database.

| Job | Schedule | Function |
|---|---|---|
| Occurrence regeneration | Daily, UTC 00:00, plus on-demand per tenant | Rebuilds `occurrences` from `event_definitions` + recurrence rules |
| Pre-event reminders | Every minute | `send_pre_event_reminders` |
| Scheduled announcements | Every minute | `send_scheduled_announcements`; independent per-`AnnouncementTarget` delivery |
| Auto-post upcoming occurrences | Daily, UTC 16:00 | `auto_post_upcoming_occurrences` (§51) |

## 7. Services Layer (`app/services/`)

- `discord_api.py` — bot-token-authenticated Discord REST client (scheduled events, channels, roles, token verify, message send)
- `discord_oauth.py` — app-level OAuth2, kept separate from `discord_api.py` as a genuinely distinct Discord auth mechanism
- `sessions.py` — signed session cookies; `SameSite=Strict` is the CSRF defense (no separate CSRF token scheme); also short-lived OAuth-flow state cookies
- `audit.py` — `log_change`, invoked at mutation points across admin routers
- `time_utils.py` — `ensure_utc` (see §4)
- `recurrence.py` — interval-in-days + anchor-date occurrence math; no named recurrence types by deliberate design
- `validators.py` — shared Pydantic field validators for `EventIn`/`EventPatch`
- `notifications.py` — SMTP email, separate from Discord notifications

## 8. Frontend

Plain HTML/CSS/JS under `app/static/`, served directly by `routers/admin/ui.py` (`admin.html`) and `routers/events.py` (`events.html`). Classic `<script src>` tags loaded in order (not ES modules), so `admin.html`'s script order is the dependency order:

`theme.js` (head, prevents flash of wrong theme) → `common.js` (`api()`, `toast()`, `showView()`, `loadMe()`, `loadTenants()` — load-bearing for everything after it) → one file per admin view (`dashboard.js`, `events.js`, `schedule.js`, `gantt.js`, `postlog.js`, `config.js`, `sync.js`, `access.js`, `platform.js`) → `init.js` (orchestrates initial load and the "logged in, no tenant access yet" empty state).

`access.js` (owner-only invite management) and `platform.js` (superadmin-only Kingdom/Tenant creation) are conditionally shown per `applyRoleVisibility()`.

## 9. Authentication and Authorization

- Discord OAuth2 is the only authentication mechanism and the only way a new user is created.
- Access is granted exclusively through `Invite` consumption — there is no self-service signup.
- Roles: tenant-level `owner`/`coordinator` (via `user_tenants`), and a separate kingdom-level `kingdom_coordinator` flag (via `user_kingdoms`) that does not derive from tenant ownership.
- Superadmins are bootstrapped exclusively from the `SUPERADMIN_DISCORD_IDS` environment variable (comma-separated Discord user IDs) — there is no other path to platform admin access.
- Sessions are signed cookies (`itsdangerous`), not bearer tokens; CSRF protection relies on `SameSite=Strict`.

## 10. Configuration (Environment Variables)

| Variable | Purpose |
|---|---|
| `DB_PASSWORD` | PostgreSQL password |
| `SECRET_KEY` | Session-cookie signing key; boot fails if unset |
| `DISCORD_OAUTH_CLIENT_ID` / `_SECRET` / `_REDIRECT_URI` | Discord OAuth2 app credentials; boot fails if client ID/secret unset |
| `SUPERADMIN_DISCORD_IDS` | Comma-separated Discord user IDs granted superadmin on first login |
| `PLATFORM_BOT_TOKEN` | Fallback bot token for tenants without their own |
| `PLATFORM_PUBLIC_KEY` | Fallback Ed25519 public key for webhook signature verification |
| `NOTIFY_EMAIL`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS` | Outbound email (SMTP, port 587 default) |

`DATABASE_URL` is not in `.env` itself; `docker-compose.yml` constructs it as `postgresql+asyncpg://taraka:${DB_PASSWORD}@db:5432/kingshot_scheduler`.

## 11. Deployment

Docker Compose defines two services:

- **`app`** — built from `./app`, binds `127.0.0.1:8000:8000` only (no direct external exposure), depends on `db` being healthy, mounts `./app` as a volume.
- **`db`** — `postgres:16-alpine`, database `kingshot_scheduler`, user `taraka`, data persisted to `./postgres`, health-checked via `pg_isready`, bound to `127.0.0.1:5432:5432`.

Caddy (reverse proxy) and Cloudflare Tunnel/Access sit in front of the app, outside this repository. The admin UI (`/admin`) is Cloudflare Access–gated at the network edge, with Discord OAuth required within. Public calendar routes are not gated at the edge.

Backup: `/opt/taraka/postgres/` is included in `restic` backups with a `pg_dump` pre-hook.

## 12. Testing (`app/tests/`)

**Superseded by the unified event model.** Tests for the replaced code were removed in §66.8; see CS.9 and `CLAUDE.md`.

In-memory SQLite via `aiosqlite`, orchestrated through `conftest.py` fixtures; never touches production Postgres.

Key fixtures: `tenant`/`second_tenant` (two tenants in one Kingdom), `test_user` (a superadmin by design, so business-logic tests aren't implicitly testing the permission model too), `client` (authenticated as `test_user`), `make_user_and_client` (factory for a specific non-superadmin user with specific grants, for permission tests).

| Test file | Coverage |
|---|---|
| `test_routes_public.py` | Unauthenticated public routes, cross-tenant isolation |
| `test_routes_events.py` | Admin event CRUD, `scope` validation, kingdom-wide cross-tenant read visibility |
| `test_post_occurrence.py` | Discord-posting race conditions/error paths; kingdom-wide fan-out |
| `test_auth.py` | Session token round-trip/tamper rejection, invite-claim race guard, full HTTP invite-claim flow |
| `test_auth_permissions.py` | Permission model: no-session → 401, no-tenant-access → 403, coordinator vs. owner, kingdom-coordinator independence, superadmin-only CRUD |
| `test_announcements.py` | Creation validation, permission scoping, delivery (partial-failure isolation), cancellation |
| `test_recurrence.py` | Recurrence math, no HTTP |
| `test_validation.py` | Schema/field validation including `scope` |

Run via: `pip install -r app/requirements-dev.txt && cd app && pytest`. `requirements.txt` is runtime-only (what ships in the Docker image); `requirements-dev.txt` layers pytest and the in-memory SQLite driver on top for local/CI use.

## 13. Admin UI — Announcements

**Superseded by the unified event model.** Replaced by §66: an announcement is now an event with no duration. Kept as design history.

**Replacement planned:** §66 (unified event model) merges announcements into events. Remains accurate for the running code until that ships.

**Status:** Implemented and deployed. All three revisions specced below (recurring delivery, the leadership/general distinction, live Discord channel dropdowns) are live: `routers/admin/announcements.py`, `scheduler/announcements.py`, the `leadership_only`/`recurring`/`interval_days` columns on `announcements` (§4), `admin.html`'s Announcements tab, and `js/announcements.js`.

### 13.1 Relationship to Events

An announcement is deliberately not an event: no start/end time, no Discord Scheduled Event object, no entry on the public calendar or ICS feed. It behaves the same way the Events form's **Leadership Only** checkbox already does for events — a channel post and nothing else — so the Announcements form reads as a sibling of that pattern rather than a new concept: "if Leadership Only turns an event into a channel-only ping, an Announcement is that same channel-only ping without an underlying event at all."

**Single creation process.** Events was revised (see the admin.html/events.js changelog) to drop its second "+ Add Leadership Event" button in favor of one "+ Add Event" button with a **Leadership Only** checkbox inside the modal — both buttons always called the same `openEventModal()` function, so the second button was a convenience shortcut, not a separate process. Announcements follows the same one-button-plus-checkbox pattern from the start: a single "+ New Announcement" button, with **Leadership Only** as a checkbox inside the one modal, not a second button.

The Announcements form still has **no** fields for: duration, notification role/minutes-before, or a scope-to-Discord-Scheduled-Event toggle. It has a title, a body, a send time, a recurrence toggle, a leadership/general flag, and one or more targets.

### 13.2 Form Fields

| Field | Maps to | Notes |
|---|---|---|
| Title | `title` | Plain text, required |
| Body (Markdown) | `body_markdown` | Multi-line, required; hard 2000-character cap enforced client-side and server-side (Discord's own message limit) — show a live character counter, matching the Event form's plain-textarea style for **Description** |
| Send Date (UTC) + Send Time (UTC) | `scheduled_for` | Two separate inputs — a `type="date"` field and a plain-text `HH:MM` field (24-hour, validated client-side with the same regex as the Event form's Start Time field) — joined client-side into one ISO 8601 UTC string before submit. Originally a single native `datetime-local` input; split apart because that control's on-screen display (12-hour AM/PM vs. 24-hour) follows the browser's OS/locale setting with no reliable cross-browser way to force 24-hour, whereas a plain text field is fully under the app's control — same reasoning already applied to the Event form's Start Time field, which never used a native time input for this reason. Stays UTC-entry regardless of §15's header time zone control — see §15.2 |
| Recurring | `recurring` | Checkbox. When checked, reveals **Repeat every (days)** |
| Repeat every (days) | `interval_days` | Integer, required only when Recurring is checked; hidden and cleared otherwise, mirroring how the Event form's fields behave conditionally (e.g. Leadership Only's note block) |
| Leadership Only | `leadership_only` | Checkbox, mirrors `event_definitions.leadership_only` exactly. Categorization/display flag only (see 13.3) — it does not restrict who can create one or gate which channels are pickable |
| Targets | `targets[]` | One or more `{tenant_slug, discord_channel_id}` pairs (see 13.3) |

No `scope` field exists for announcements — targeting is handled entirely by the Targets list, not by an alliance/kingdom-wide toggle.

**Past-date rejection.** `POST /api/announcements` rejects a `scheduled_for` at or before the current time with 422 (`routers/admin/announcements.py`), checked both server-side and client-side (`saveAnnouncement()` in `js/announcements.js`, immediately after the HH:MM format check, using the same joined UTC timestamp the payload will submit). There is no equivalent check on `EventDefinition.anchor_date`: an event's anchor date is legitimately allowed to be in the past — that's how a recurring event's occurrences are generated forward from it — and `post_occurrence`'s existing "can't post an occurrence starting less than 15 minutes from now" guard already prevents a stale/past occurrence from actually going out, which is the equivalent protection for events.

### 13.3 Target Selection

Each target is a `(tenant, Discord channel)` pair, added one at a time:

- A tenant picker limited to tenants the current user has `UserTenant` access to (owner or coordinator) — mirrors `check_target_access`'s server-side enforcement (`routers/admin/deps.py`, shared with Events' target validation — see §20), so the UI should not even list tenants the API would reject with 403. Superadmins may additionally see and pick any tenant, not just their own.
- **Live Discord channel dropdown**, replacing the earlier free-text channel ID field. No backend change is needed: `GET /api/discord/channels` already exists and already scopes to whichever tenant is named in the request's `X-Tenant-Slug` header — exactly how the Event form's own channel dropdowns work today. Each target row, on choosing or changing its tenant, fetches that tenant's channel list using the same per-request tenant-header-override mechanism already built for combined-mode writes (`api()`'s `tenantOverride` parameter in `common.js`) and populates a real `<select>`, falling back to the manual text field only if the fetch fails (matching `populateDiscordFields()`'s existing fallback behavior in the Event modal).
- `leadership_only` does not change which channels are offered or which tenants are selectable — the same channel list is shown either way. It is purely how the announcement is categorized once created (13.4), not how it is targeted.
- "Add another target" lets one announcement fan out to multiple alliances (e.g. a kingdom-wide leadership notice) in a single create call, each delivered and tracked independently per §9's per-target resilience model.

### 13.4 List / Status View

A table below the form, scoped to `GET /api/announcements` (announcements this tenant is either the owner or a target of):

| Column | Source |
|---|---|
| Title | `title`, with a 👑/🛡️ badge from `leadership_only`, matching the icon convention already used on the Events, Schedule, and Post Log tables |
| Scheduled for | `scheduled_for` (next fire time; for a recurring announcement this advances after each send — see 13.5), shown in the same dual UTC + local-time format as events (§15.2) |
| Recurring | A badge or icon when `recurring` is true, showing the `interval_days` value (e.g. "every 7 days") |
| Status | `status` (`draft` / `scheduled` / `posted` / `failed` / `cancelled`) |
| Targets | one row or badge per target, showing `post_status` (`pending` / `posted` / `error`) and `status_detail` on hover for errors |
| Actions | **Cancel** button, enabled only while `status == "scheduled"`, calling `POST /api/announcements/{id}/cancel`. For a recurring announcement this cancels the whole series, not just the next occurrence — see 13.6 |

Because delivery is independent per target, an announcement with mixed target statuses (e.g. posted to one alliance, errored on another) is a valid, expected state and should render per-target status rather than a single rolled-up badge.

### 13.5 Recurring Delivery Semantics

The smallest change that fits the existing pattern, rather than a parallel occurrence-generation system: `Announcement` gains `recurring` (bool, default false) and `interval_days` (int, nullable), reusing the same "interval in days from an anchor" concept `EventDefinition` already uses for events.

In `scheduler/announcements.py`'s per-minute job, when a due announcement has `recurring = true`:

- Instead of going terminal (`status` → `posted`/`failed`) once every target has been attempted, the job resets all of that announcement's `AnnouncementTarget` rows back to `post_status = "pending"`, advances `scheduled_for` by `interval_days`, and leaves `status` as `"scheduled"` — so it fires again on its next due cycle, the same way it fired the first time.
- A non-recurring announcement (`recurring = false`, the default, matching every announcement created before this change) behaves exactly as today: terminal `posted`/`failed` after the one send.
- Cancelling a recurring announcement (13.4's Cancel button) ends the series outright (`status → "cancelled"`), not just the next occurrence — there is no "cancel just this one instance and keep the series running" in v1.

**Out of scope for v1:** a fixed occurrence count or an end date for a recurring series (e.g. "repeat 10 times" or "repeat until March"). The interval-only model matches what was asked for; a bounded series is a natural follow-up if needed later, but adds another field and another piece of scheduler logic that wasn't requested.

**Known issue (2026-10-01, diagnosis only, no fix designed).** Recurring announcements were reported to deliver the first round and then not post again. A static review found the re-arm logic correct and covered by `test_recurring_announcement_rearms_instead_of_going_terminal`, so the cause is expected in production data, scheduler runtime, or an unhandled failure. Two structural hazards were confirmed regardless of cause: the delivery tick runs inside one `try`/`except` with a single rollback, so one failing announcement blocks every announcement after it in that tick; and the Discord send and the database commit are not atomic, so a rollback after a successful send can cause a duplicate post on the next tick. See Part I, section CS.11. **Resolution (2026-10-01):** the cause will not be diagnosed. Announcements are being replaced by the unified event model (§66), which removes this recurrence implementation.

### 13.6 Out of Scope

- Draft/save-without-scheduling workflow. Not needed: creation always goes straight to `status="scheduled"`, and the `draft` value in the schema stays unused.
- Editing a scheduled announcement's body/time/recurrence before it fires. Not needed: cancel-and-recreate is the intended workflow, matching the API's cancel-only surface (no update endpoint). (Built in §49 — `PATCH /api/announcements/{id}` now supports in-place editing while `status == "scheduled"`.)
- Rich Discord embeds (the API sends plain markdown text via the same `send_channel_message` path used for occurrence pings).
- Gating `leadership_only` behind a permission check (e.g. restricting it to owners or kingdom coordinators). It is a display/categorization flag only, same as it is for events.

## 15. Time Zone Display — Header Control (Admin) and Public Events Page

**Status:** Implemented and deployed. Fixed a real usability gap that existed before this: the only display-timezone control in the app today is the **Display Timezone (admin UI)** dropdown (`mTimezone`) buried inside the Add/Edit Event modal — a place a user would only ever open to create or edit an event. Yet `getDisplayTz()` (`events.js`), which that dropdown writes to, is read globally: Dashboard's "Today's Events" times, Schedule's occurrence times, Gantt's chart axis, and Post Log's "Posted At" column all format through it. Worse, `getDisplayTz()`'s fallback when nothing is stored yet is a hardcoded `'UTC'`, not the browser-detected zone — even though "Your detected timezone: ..." is shown as informational text right next to the control. A new user sees their own zone named and still gets UTC everywhere until they happen to open the Event modal and pick it themselves. On top of relocating that control, this section also changes *what* is displayed: everywhere an event time is shown, in the admin area or on the public events page, both the canonical UTC time and the viewer's detected/selected local time are shown together — not one replacing the other, as the original draft of this section specced.

### 15.1 Approach (Admin)

Relocate the control to the masthead header (`admin.html`, alongside the tenant picker and the Log out button), so it is visible and reachable from every tab, not just from inside the Event modal. Two behavior changes alongside the move:

- **Default changes from hardcoded UTC to browser-detected.** The first time a user visits with nothing in `localStorage`, the control defaults to `Intl.DateTimeFormat().resolvedOptions().timeZone` (the same detection already used for the Event and Announcement modals' "Your detected timezone" helper text), not `'UTC'`.
- **Persistence is unchanged.** The override still lives in `localStorage` under the existing `samaya_display_tz` key — an existing stored value from a previous session keeps working with no migration, since it's the same key just read/written from the header instead of the modal.

The Event modal's own `mTimezone`/`mDetectedTz` fields are removed once the header control exists — they become redundant, not a second source of truth.

### 15.2 Dual-Time Display Format

Every place an event, occurrence, or announcement time is shown — Dashboard, Schedule, Gantt (with the exception in 15.5), Post Log, Announcements (§13.4's "Scheduled for" column), and the public events page (15.4) — shows **both** the UTC time and the viewer's detected/selected local time, not one or the other. Concretely, `fmtTime`/`fmtDateTime` (`events.js`) change from swapping the displayed value to concatenating both:

- `19:00 UTC` when the selected/detected zone *is* UTC (no duplication — showing "19:00 UTC · 19:00 UTC" would be noise, not information).
- `19:00 UTC · 3:00 PM EDT` when it differs — UTC stays in its existing 24-hour `HH:MM` form (unchanged, still the canonical value everything else in the app already keys off), and the local rendering uses 12-hour clock plus the zone's abbreviation (via `Intl.DateTimeFormat(..., { timeZoneName: 'short' })`) so the two are visually distinct at a glance rather than two bare numbers that only differ by an hour count.

**Dates can differ, not just times, near midnight UTC** — an event at `23:30 UTC` on the 28th is `7:30 PM EDT` on the *same* calendar day, but one at `01:00 UTC` on the 28th is `9:00 PM EDT` on the *27th*. `fmtDateTime` (used by Post Log's "Posted At" column and anywhere else a date accompanies the time) must render each half against its own correct calendar date rather than assuming both halves share the date already shown once — this is a real correctness detail, not just a formatting choice, and worth testing explicitly against a near-midnight occurrence.

**Entry fields are unaffected and stay UTC-only**, per instruction — events are still created in UTC: the Event form's **Start Time (UTC)** field and the Announcement form's **Send at (UTC)** field (§13.2) keep their UTC labeling and single value, and are not given a dual-time treatment. Dual-display is for showing *existing* timestamps, never for entering new ones.

### 15.3 Header Control (Admin)

The header shows a live UTC/local clock as a clickable text link, opening a Time Zone modal listing the same IANA zones — see §24 for the control's actual shape. The zone list, default-to-detected behavior, and `localStorage` key are as specified in 15.1.

### 15.4 Public Events Page

See §62 for the public events page's current time zone control (a `timezoneModal` opened from the header clock button). The underlying behavior — detect-by-default, override via the shared `samaya_display_tz` `localStorage` key, dual-format display per 15.2 — is unchanged from this section's original intent.

### 15.5 Gantt Exception

Gantt's day cells (`static/js/gantt.js`) are small, fixed-width grid cells with room for one short time label (`fmtTimeShort()` today), not a full sentence — cramming a dual-time string into every cell would make the grid unreadable. Gantt keeps a single short label inline (the local/selected time, since that is what a viewer scanning their own week most wants at a glance), and puts the full dual-time string in the cell's existing `title` tooltip attribute instead (already used today for the event name and date, extended to include the dual time on hover). This is the one view where "shown" means "on hover" rather than "inline," because of real space constraints, not an oversight.

### 15.6 Out of Scope

- Per-view timezone overrides (e.g. Schedule shown in one zone, Post Log in another). One global setting is what's being asked for.
- A dedicated "reset to detected" action. Switching back to the detected zone is just picking it from the same dropdown like any other zone; no separate control is needed.
- Applying the dual-time treatment to the Event/Announcement UTC-only **entry** fields (Start Time, Send At). Display and entry stay distinct per 15.2 — Announcements' **display** column (§13.4's "Scheduled for") is in scope and covered above.

## 17. Admin UI — Tab Order

**Status:** Implemented and deployed, then further reshaped by later sections. The tab bar is ordered so the tabs a coordinator actually works in day to day come first, and the tabs that configure or administer the system come last. The order this section originally specified was:

`Dashboard → Events → Announcements → Schedule → Gantt → Post Log → Sync → Config → Access → Platform`

That list is superseded in two ways by later sections: Gantt is no longer its own tab — it became a second layout inside the Schedule tab (§38.3, "Gantt shares Schedule's own filter... since it's a second layout over the same occurrence window"), and Access/Platform merged into one "Access & Platform" tab (§38.6), with Tickets (§43) and Audit Log (§31.1) added afterward. The current tab order is:

`Dashboard → Events → Announcements → Schedule (Table/Timeline toggle) → Post Log → Sync → Config → Tickets → Access & Platform → Audit Log`

Rationale for the original grouping still holds for the tabs it covers: Events and Announcements are adjacent since both are coordinator-authored, scheduled content (§20's shared target-validation logic); Schedule/Post Log are the "what's actually happening" occurrence-tracking group; Sync/Config are tenant-level admin; Access & Platform and Audit Log are last, being the least-frequently-used, highest-privilege (superadmin-only, §38.6) tabs. No functional change from reordering alone — `showView()`/`applyRoleVisibility()` in `common.js` look up tabs by `id`, not position.

## 19. Config — Server Name Column

**Status:** Implemented and deployed. The Config tab's Channels table only ever showed a channel's name and Discord ID, with no indication of which physical Discord server it belongs to — a gap that matters because a **Tenant** (alliance) and a **Discord guild** are not one-to-one: MOD and NSR, for example, share one `guild_id`, and HTD is described as the de facto Kingshot-wide server. Added:

- `services/discord_api.get_guild_info(token, guild_id)` — calls `GET /guilds/{guild_id}`, returning the guild's own Discord display name (distinct from `Tenant.name`, which is the alliance's name in our system, not the Discord server's).
- `GET /admin/api/discord/guild` (`routers/admin/discord_config.py`) — exposes it, scoped to the current tenant the same way `/api/discord/channels`/`/api/discord/roles` already are.
- If the guild-info fetch fails (502 — most commonly because the bot hasn't been invited to that guild yet, Discord returning 403), the resolved server info is simply left out rather than failing the whole channel/role listing, since those can still load successfully or fail independently.

**Admin UI superseded by §38.5.** The single-tenant Channels table with a Server column (described above as originally built) was replaced by a Discord Config tab that groups by `DiscordServer` instead of by Tenant — a `<details>` accordion section per server, since more than one alliance can share one server and a flat per-tenant table would repeat that server's identical channel/role list under each alliance's name. `get_guild_info`/`GET /api/discord/guild` (above) is still the function that resolves a server's display name; it's now called from `GET /api/discord/config-overview` (§38.5) rather than rendered as a per-row table column.

## 20. Events — Multi-Server Notification Targets

**Superseded by the unified event model.** Replaced by `event_alliances` (§66.1, §66.2).

**Replacement planned:** §66 replaces event targets with `event_alliances`. Remains accurate for the running code until that ships.

**Status:** Implemented and deployed. Originally specced as a Discord-*posting* multi-target mechanism mirroring Announcements' (`{tenant_slug, discord_channel_id}`); revised during implementation once it became clear the actual complaint driving this (event notifications reaching only one channel/role) is about the **pre-event ping**, not the Discord Scheduled Event post itself — the ping is the only piece that's genuinely per-guild-channel-scoped. The event's own Discord Scheduled Event `location` field (`discord_channel`) is free text reused identically across every target's guild already (see kingdom-wide fan-out, unchanged), so there was nothing to generalize there.

### 20.1 Why this is additive, not a replacement

`scope` (`alliance`/`kingdom-wide`) and its automatic fan-out to same-Kingdom tenants (§3, `occurrences.py`'s `_post_to_one_tenant`) stays exactly as it is — it's live production behavior serving real kingdom-wide events today, and collapsing it into a manually-maintained target list would mean every existing kingdom-wide event silently stops fanning out to a *new* alliance that joins the Kingdom later (the automatic version re-evaluates Kingdom membership every time occurrences generate; a stored target list would be a one-time snapshot). Instead, this adds a **second, independent** targeting mechanism on top:

- A new `event_targets` table (`models.db.EventTarget`): `id`, `event_id` (FK → `event_definitions`, `ON DELETE CASCADE`), `tenant_id` (FK → `tenants`), `notification_channel_id`, `notification_role_id` (both `TEXT NOT NULL DEFAULT ''`). Unique on `(event_id, tenant_id)`. Migration: `migrate_add_event_targets.py`.
- These are **explicit extra destinations**, independent of `scope` and of Kingdom membership entirely — this is the HTD case: HTD is a community-wide server that isn't necessarily a Tenant inside the same Kingdom grouping as the alliances whose events should also reach it, so it needs to be addressable regardless of Kingdom.
- **Posting** (`post_occurrence` in `occurrences.py`): the target list for a given occurrence is now `[owning tenant]` (alliance-scope) or `[every same-Kingdom tenant]` (kingdom-wide, automatic, unchanged) **plus** every tenant with an `EventTarget` row for that event, deduplicated by tenant id before posting — a tenant that's both a kingdom-wide fan-out recipient and an explicit target gets exactly one Discord Scheduled Event and one `PostLog` row, not two. When there's exactly one resulting target (the common case: an alliance-scope event with no explicit targets), the endpoint keeps its original single-target response shape (`{discord_event_id, status}` with a 409/502 on failure) rather than always returning the multi-target `{status, targets: [...]}` shape — no response-shape change for the overwhelmingly common case.
- **Notification resolution** (`_resolve_notification` in `occurrences.py`): the owning tenant always uses the event's own bare `notification_channel_id`/`notification_role_id`. A kingdom-wide fan-out tenant checks `EventTenantNotification` first (existing behavior, unchanged), then falls through to check `EventTarget` — so a tenant can be reached via *either* mechanism, or both, with `EventTarget` as the catch-all for anything `EventTenantNotification` doesn't cover (any tenant that isn't the owner and isn't a kingdom-wide fan-out recipient goes straight to `EventTarget`). No row in either place for a target = no ping for that target, same as before — the Scheduled Event post itself still happens regardless.

### 20.2 UI changes

- The Event modal gains an **Extra Notification Targets** section: a tenant picker plus a live per-tenant channel dropdown *and* a live per-tenant role dropdown (`addEventTargetRow`/`populateEventTargetFields` in `events.js`) — one more field than Announcements' target row (§13.3), since an event's ping carries a role mention where an announcement's plain channel post doesn't need one.
- `EventIn` gains `targets: list[{tenant_slug, notification_channel_id, notification_role_id}] = []`; `EventPatch` gains the same field as `Optional[...] = None` (`None` = leave the current list alone, `[]` = explicitly clear it — the admin UI always sends the full current list on save, so in practice every save is a full replace, never a partial merge).
- Both Announcements' and Events' server-side target validation (resolve tenant slugs, check the caller holds `UserTenant` access to each) now share one implementation — `resolve_target_tenants`/`check_target_access` in `routers/admin/deps.py` — rather than each endpoint keeping its own copy of the same check.

### 20.3 Out of scope

- A separate `discord_channel_id`-per-target field for the Scheduled Event post itself — not needed, since that field is decorative location text already reused identically across every guild (see above).
- `notify_minutes_before` per target — every target uses the event's own single lead time; only the destination (channel/role) varies per target, not the timing.
- Migrating existing kingdom-wide events into `EventTarget` rows. Not needed — §20.1 keeps the automatic fan-out mechanism intact rather than replacing it, so there's nothing to migrate.

## 21. Access Management — Editing Existing Grants, Kingdoms, and Superadmin Status

**Status:** Implemented and deployed. Prior to this, editing "configuration data" stopped at the Tenant level (§18): once someone claimed an invite, there was no way to change their role, remove their access, edit a Kingdom's own name/slug, or manage the platform-wide superadmin flag — only the invite *lifecycle* (create/revoke a still-pending invite) was exposed anywhere. Closed four separate gaps:

### 21.1 Tenant members (UserTenant grants)

- `GET /admin/api/members` — everyone with standing access to the current tenant (owner-scoped, same `require_tenant_owner` dependency as invite management), returning each grant's role alongside the user's Discord username. The counterpart to `GET /api/invites`, which only ever showed pending/used/revoked invite *records*, never the resulting grant.
- `PATCH /admin/api/members/{id}` — change a grant's role between `owner`/`coordinator`.
- `DELETE /admin/api/members/{id}` — remove access outright (the person needs a fresh invite to get back in).
- Guard: a tenant can't be left with zero owners — demoting or removing the *only* remaining owner is rejected with 400, since that would leave no one able to invite, edit tenant config, or manage access at all. Promote a second owner first.
- Admin UI: the Access tab gains a **Members** table above the existing Invites table, with "Change role" and "Remove" buttons per row (`access.js`). The Access tab itself was owner-only at the time this was written; §38.6 later made it (and Platform, merged into the same "Access & Platform" tab) superadmin-only in the UI, though the backend endpoints here are still gated by `require_tenant_owner`, unchanged.

### 21.2 Kingdom coordinators (UserKingdom grants)

- `GET /admin/api/kingdom-coordinators?kingdom_id=` and `DELETE /admin/api/kingdom-coordinators/{id}` (`routers/admin/invites.py`, superadmin-only) — the standing-grant counterpart to the existing kingdom-invite endpoints, mirroring §21.1's tenant-member pattern.
- Admin UI: a **Kingdom Coordinators** table in the Platform section (nested inside the "Access & Platform" tab since §38.6; `platform.js`'s `loadPlatformKingdomCoordinators()`), sitting between the Kingdoms and Tenants tables. Since the list endpoint is scoped to one kingdom at a time, the loader fans out one `GET /api/kingdom-coordinators` call per kingdom (from the already-loaded kingdom list) and flattens the results into one table with a Kingdom column, rather than adding a per-kingdom expandable row. Each row's **Remove** button calls `removeKingdomCoordinator()`, gated behind a `confirm()` naming the user and kingdom, matching §21.1's Members table and §21.4's Users table conventions.

### 21.3 Kingdom editing

- `PATCH /admin/api/kingdoms/{id}` (`routers/admin/tenants.py`, new `KingdomPatch` schema, superadmin-only) — Kingdoms previously supported creation only. Now editable the same `name`/`slug` fields as creation.
- Admin UI: an **Edit** button on each Kingdoms row in the Platform tab (`editKingdom()` in `platform.js`), same `prompt()`-based, Cancel-leaves-unchanged convention as `editTenant()` (§18).

### 21.4 Platform-wide user management and superadmin flag

- New `routers/admin/users.py` (superadmin-only): `GET /api/users` lists every `User` row platform-wide (Discord username, superadmin flag, last login); `PATCH /api/users/{id}` toggles `is_superadmin`.
- Kept separate from `invites.py` deliberately — this edits the `User` row itself, not a per-tenant/per-kingdom grant, and its effect crosses every tenant/kingdom boundary at once rather than being scoped to one.
- Guard: a superadmin cannot revoke their own superadmin flag (400) — since the very next request would fail `require_superadmin` with no UI path back in short of direct database access. Another superadmin can still demote them.
- Admin UI: a **Users** table in the Platform section (nested inside "Access & Platform" since §38.6; `platform.js`), each row showing Discord username, a Superadmin label when set, last login, and a "Make superadmin"/"Revoke superadmin" button — gated behind a `confirm()` describing the scope of what's being granted, since it's the single highest-privilege action in the system. The current user's own row shows `(you)` instead of a revoke button rather than letting them hit the server-side guard.

### 21.5 What's still out of scope

- No bulk operations (e.g. removing several members at once) — every action here is single-row, matching the rest of the admin UI's `prompt()`/`confirm()`-based conventions rather than introducing a new selection/bulk-action pattern for this alone.
- `discord_id`/`discord_username` on `User` remain read-only everywhere — they come from Discord OAuth and aren't meant to be hand-edited.

## 22. Known Gaps and Design Notes

- No per-tenant custom Discord Interactions Endpoint support — all inbound webhooks go through one platform-level endpoint verified against `PLATFORM_PUBLIC_KEY`.
- No named recurrence types (weekly, monthly, etc.) — recurrence is purely interval-in-days from an anchor date.
- ~~Alembic is declared as a dependency but unused.~~ Corrected: Alembic is now the only schema-change convention (§65). The hand-written scripts remain in the repo as a record only.
- Audit log is append-only and explicitly not a rollback mechanism, since many logged actions have already reached Discord by the time they're logged.
- A separate architecture document ("Samaya Self-Hosted Architecture PRD v2.0", Google Drive) predates the multi-tenant/auth rework and is superseded by the repository's own `CLAUDE.md`, which this specification is derived from alongside direct inspection of the source.

## 23. Deployment — Static Asset Cache-Busting

**Status:** Implemented and deployed. `ks138.taraka.dev` sits behind Cloudflare, which caches `/static/*.js` and `/static/*.css` at the edge by file extension for hours (`Cache-Control: max-age=14400` observed) independent of how fresh the origin's copy actually is — a redeployed JS file can keep serving its pre-deploy content to every visitor until that edge cache entry expires or is purged, and a client-side `fetch(..., {cache: "no-store"})` does not bypass it (that header only controls the browser's own cache, not an intermediary's). `/` itself wasn't affected, since Cloudflare's default rules don't cache HTML by file-extension the way they do JS/CSS, and `admin_home()` reads and returns `admin.html` fresh on every request anyway.

Fixed at the origin rather than relying on a manual Cloudflare purge after every deploy: `routers/admin/ui.py` now rewrites every `/static/...` `src`/`href` reference in `admin.html` to append `?v={STATIC_ASSET_VERSION}` before serving it (`_bust_static_cache()`, a small regex substitution — skips any reference that already carries a query string, and only touches local `/static/` paths, not external vendor CDNs). `STATIC_ASSET_VERSION` is a constant in that file, bumped whenever a static asset actually changes; because it's baked into the URL rather than the file itself, a version bump makes Cloudflare (and browsers) treat every asset as a new object and fetch it from origin, with no dashboard purge required. It's tracked independently of the spec/API version number — it only needs to move when `/static` content changes, not on every release.

Covered by `tests/test_ui.py` (`_bust_static_cache()` unit tests — an HTTP-level test of `GET /` isn't practical since `admin_home()` reads a hardcoded `/app/static/admin.html` path that only exists inside the built container).

**Out of scope:** the public events page (`/events`, `/t/{slug}/events`) doesn't reference any of its own `/static/js/*.js` files (only pinned vendor CSS), so it isn't exposed to this problem and wasn't touched.

## 24. Admin UI — Header Controls as Modals, and Announcement Cleanup

**Status:** Implemented and deployed. Two changes bundled together since both landed in the same pass:

### 24.1 Alliance and Time Zone — Text Link + Modal, Replacing the Header `<select>`s

The masthead's two `<select>` dropdowns (the tenant picker and, per §15.3, the time zone selector) are replaced with two plain text links (`#tenantLink`, `#headerClock` in `admin.html`), each showing the current state at a glance and opening a small modal to change it:

- **`#tenantLink`** shows the current alliance's name (or "— All my alliances —" in combined mode, §14) followed by "▾", and opens the **Switch Alliance** modal (`#tenantModal`) — a plain list of buttons, one per tenant the user has access to plus the combined option when there's more than one, mirroring the old dropdown's option list exactly. Selecting one calls the same `onTenantChange()` the old `<select>`'s `onchange` called, so every downstream behavior (role-visibility, single-tenant-only view correction, view reload) is unchanged — only the picker UI moved.
- **`#headerClock`** shows a live clock in the form `🕐 HH:MM UTC` (UTC only when the selected/detected zone *is* UTC) or `🕐 HH:MM UTC · HH:MM (Zone/Name)` otherwise, ticking on a 30-second interval — plenty for a minute-resolution display without needless re-renders. Clicking it opens the **Time Zone** modal (`#timezoneModal`), the same IANA zone list §15.3 originally put in a dropdown, now as a list of buttons; selecting one calls the same reload logic `onDisplayTzChange()` used to (renamed `selectTimezone()`), re-rendering every dual-time view exactly as before.
- Both modals follow the existing `#eventModal`/`#announcementModal` show/hide convention (a per-ID `display:none` / `.open{display:block}` CSS pair, since PatternFly's own backdrop has no built-in show/hide state) rather than introducing a new modal mechanism.
- `startHeaderClock()` runs immediately on page load, independent of login/tenant state (same reasoning §15.1 gave for the old picker: a user waiting on a delayed login redirect still sees a correctly-running clock), while `loadTenants()` still waits on `loadMe()` since it needs to know which alliances this user can see.

### 24.2 Announcements — Cleanup and Duplicate

Addresses a real gap: once an announcement reaches a terminal state (`posted`/`failed`/`cancelled`), it had no way to leave the list, and a cancelled announcement's Targets column kept showing each target's stale pre-cancellation `post_status` (usually `pending`), which read as if the cancellation hadn't taken effect.

- **`DELETE /admin/api/announcements/{id}`** (`routers/admin/announcements.py`) removes an announcement outright — allowed only when `status` is `posted`, `failed`, or `cancelled` (400 otherwise, telling the caller to cancel it first); a still-`scheduled` announcement keeps using the existing Cancel action, not this one. `AnnouncementTarget` rows cascade via the FK's existing `ondelete="CASCADE"`.
- Admin UI: a **Delete** button in the Actions column for any announcement in one of those three terminal states, alongside the existing **Cancel** (scheduled only) and a new **Duplicate** (always available).
- **Duplicate** opens the New Announcement modal pre-filled with the source announcement's title, body, leadership flag, recurrence settings, and full target list (each target's tenant + Discord channel, re-resolved through the same live-channel-dropdown mechanism §13.3 already uses) — except the send date/time, which is always left blank. A duplicate's whole point is scheduling the same content again; carrying forward the original's date/time would either be in the past already or reproduce whatever got it cancelled/failed in the first place.
- The Targets column now shows `cancelled` for every target of a cancelled announcement, rather than each target's individually-tracked `post_status` — once the parent is cancelled, the per-target delivery attempt is moot regardless of what it was before that.

### 24.3 Out of Scope

- Bulk delete/duplicate across multiple announcements at once — every action here is single-row, matching the rest of the admin UI's established convention (§21.5 made the same call for Access Management).
- Deleting a `scheduled` announcement directly (skipping Cancel) — kept as a two-step action deliberately, so a live announcement is never removed without the explicit cancellation step first.

## 25. Discord Server as a First-Class Entity

**Status:** Implemented, pending deployment. All bullets below describe what was built; §25.1's migration script must run against production before the new application code is deployed (same order as every other schema-changing migration in this repo).

**Problem.** `Tenant` (alliance) has carried `guild_id`/`bot_token`/`public_key` directly since Phase 4, and its own docstring already notes these can repeat across rows — MOD and NSR sharing one Discord guild is real and already happens (§19). But nothing makes that sharing *explicit*: each alliance sharing a guild still carries its own separate copy of the guild's bot credentials, there's no single place that lists "these three alliances live on this one Discord server," and conceptually the app conflates two things that are only loosely related — **which alliance(s) a Discord server hosts** (infrastructure) and **who can create/manage events and announcements** (an alliance-scoped concern that should reference a server, not double as its own copy of one).

**Design (confirmed):**

- New `discord_servers` table: `id`, `name` (display name), `guild_id` (unique), `bot_token` (nullable → platform-bot fallback, unchanged fallback behavior from today), `public_key` (nullable → platform fallback), `created_at`. Not scoped to a `Kingdom` — a server (HTD is the running example, described in §19 as a de facto Kingshot-wide hub) can host alliances that span Kingdoms, or none at all beyond the ones actually created there.
- `tenants` loses `guild_id`/`bot_token`/`public_key`; gains `server_id` (FK to `discord_servers`, not null). Every other Tenant field (`kingdom_id`, `name`, `slug`, `color`) is unchanged — a Tenant is still the alliance/access-control unit, it just references its Discord identity instead of carrying a copy of it.
- **One bot per server** (confirmed): once alliances share a `server_id`, they share that server's single `bot_token`/`public_key` — no per-alliance override once sharing is in effect. This is enforced structurally by the fields moving off `Tenant` entirely, not by a runtime check.
- **Access model** (confirmed, with one narrowing worth confirming explicitly): being a coordinator/owner of any alliance on a server continues to grant exactly the operational access a coordinator already has today — selecting that server's channels/roles when targeting an event or announcement, and seeing them in the Config tab (§19) — no new grant required, matching current behavior since this is unchanged from how Tenant-scoped access already works. **Narrower than a literal reading of "server access," by design**: editing the *server's own* `bot_token`/`public_key`/`name` stays superadmin-only, the same privilege level `PATCH /api/tenants/{id}`'s `bot_token` field already requires today (§18) — a credential is not day-to-day alliance config, and loosening that to "any coordinator on a shared server" would be a real privilege expansion beyond what was asked. Flagging this interpretation for explicit sign-off before building, since it's the one place a judgment call was needed beyond the three confirmed answers above.
- `EventTarget`/`AnnouncementTarget` schemas are **unchanged** — targeting stays keyed by `tenant_slug` (resolved through `resolve_target_tenants`/`check_target_access`, §13.3/§20), since that's also the access-control anchor: a user can only ever pick tenants they hold `UserTenant` access to. The actual Discord destination those resolve to now comes from `tenant.server` instead of the tenant row directly, but that's invisible to the targeting mechanism itself.

### 25.1 Migration Plan

Hand-written, idempotent script (no Alembic in this repo, per established convention — §22), run once before the app restarts on that deploy: (Historical: since §65, new schema changes use Alembic revisions instead.)

1. For each distinct `guild_id` currently on `tenants`, create one `discord_servers` row. Name it after the first tenant found with that `guild_id` (a human can rename it afterward via the new Platform UI).
2. **Bot token conflict resolution**: if multiple tenants share a `guild_id` and have *different* non-null `bot_token` values today (a real possibility, since nothing enforced consistency before this), the migration cannot silently pick one — it logs every such conflict (guild_id, the differing tenant slugs, and which token it chose) to stdout for manual review, and defaults to the **first non-null token found**, ordered by `tenant.id`. This is a real data-quality question that needs eyes on the actual production values, not something to guess safely from here.
3. Set every `tenant.server_id` to its resolved server's id.
4. Drop `tenants.guild_id`, `tenants.bot_token`, `tenants.public_key`.

### 25.2 Affected Call Sites

The pattern `tenant.bot_token or PLATFORM_BOT_TOKEN` / `tenant.guild_id` is repeated across `routers/admin/deps.py` (`get_discord_config`), `routers/admin/discord_sync.py`, `routers/admin/discord_config.py`, `routers/admin/status.py`, `routers/admin/occurrences.py`, `scheduler/announcements.py`, and `scheduler/reminders.py` — roughly 15 call sites across 7 files. Rather than auditing and modifying every individual query that fetches a `Tenant` to eager-load its new `server` relationship (real risk of a missed spot reintroducing the `MissingGreenlet` class of bug this session already hit twice building §20), `Tenant.server` is mapped `lazy="joined"` — eager by default on every fetch, everywhere, with no per-query changes required. The relationship is a single cheap FK join with no N+1 concern, so there's no real cost to always including it. Each call site's own change is then mechanical: `tenant.bot_token` → `tenant.server.bot_token`, `tenant.guild_id` → `tenant.server.guild_id`.

### 25.3 Admin UI Changes

- **Platform tab**: a new **Discord Servers** table (superadmin-only, same tier as Kingdoms/Tenants/Users today) — create/edit a server's `name`/`guild_id`/`bot_token`, listing which tenants currently reference it (read-only membership list; reassigning a tenant to a different server happens via editing the *tenant*, not the server, to keep "who owns this relationship" unambiguous).
- **Tenant create/edit** (`createTenant()`/`editTenant()` in `platform.js`): the inline "Discord guild (server) ID" and "Bot token" prompts are replaced with a single "Discord Server" selection — pick an existing server from the list, or create a new one inline (same flow `createTenant()` already uses for picking a Kingdom). `has_own_bot_token` in `_tenant_dict()` is retired along with the field it described; the Tenant row no longer has an opinion on bot credentials at all. **The `prompt()`-chain mechanism itself is superseded by §38.7**, which replaced Tenant create/edit with a real modal (`openTenantModal()`/`saveTenantModal()`) — the Discord Server `<select>` described here is still exactly how a server is picked, just inside that modal instead of a prompt.
- **Config tab** (§19): unchanged in appearance — still shows that tenant's server's channels/roles/Server-name column — since it already reads through `get_discord_config`, which becomes the one place that changes underneath it.

### 25.4 Out of Scope

- Letting a non-superadmin move a tenant to a different server, or edit a server's credentials directly — both stay superadmin-gated per 25's access-model note above.
- Any change to how `EventTarget`/`AnnouncementTarget` resolve access or store their destination — unchanged, per the Design section above.
- Retiring `has_own_bot_token`'s underlying concept everywhere at once — this section covers `Tenant`/`DiscordServer` only; if some other part of the codebase reads that flag beyond `_tenant_dict()`/`platform.js`, it surfaces during implementation, not guessed at here.

### 25.5 Implementation Notes

Built exactly as designed above, with one addition: `GET/POST/PATCH /admin/api/discord-servers` (`routers/admin/tenants.py`) — superadmin-only, mirroring the existing Kingdom/Tenant endpoint shape, backing the new Platform-tab table. `TenantIn`/`TenantPatch` now take `server_id` in place of `guild_id`/`bot_token`/`public_key`; `_tenant_dict()` returns `server_id`/`server_name`/`guild_id` (read-only convenience, sourced from the joined `DiscordServer` row) and no longer exposes `has_own_bot_token` or any credential field.

Covered by `tests/test_discord_servers.py`: CRUD + superadmin gating on the new endpoints, Tenant create/update now going through `server_id` (including the 404 on an unknown server), and — the actual behavior this redesign exists for — two tenants sharing one `DiscordServer` and a bot-token update on that server being visible to both, with no per-tenant override to go stale. `tests/conftest.py`'s `tenant`/`second_tenant` fixtures and `tests/test_event_targets.py`'s third-tenant helper were updated to create a `DiscordServer` per tenant (each still gets its own server, preserving every existing test's assumption of separate guilds/tokens per tenant — sharing is exercised only by the new tests above).

One SQLAlchemy async wrinkle worth flagging for future call sites: `Tenant.server` is `lazy="joined"`, which means it's already populated the moment a `Tenant` is first loaded in a session — but if that same in-session `Tenant` object's `server_id` is then changed and committed, a follow-up `select(Tenant)...` for the same row returns the *same identity-mapped Python object* with its now-stale `.server` still attached (a plain re-select only populates attributes that were never loaded). `update_tenant` handles this with an explicit `await db.refresh(tenant, attribute_names=["server"])` after changing `server_id`; any future code path that reassigns `Tenant.server_id` mid-session needs the same explicit refresh, not just a re-query.

## 26. Admin Logout Contrast, Public Events Page Header Parity, and Alliance Switcher

**Status:** Implemented and deployed.

### 26.1 Admin — Log Out Button Contrast

The masthead's Log Out button uses PatternFly's `pf-m-secondary` button variant, whose default color tokens (`--pf-v6-c-button--m-secondary--Color`/`--BorderColor` and hover variants) are tuned for a light background — on the dark masthead it rendered as a low-contrast blue-outlined button, hard to read at a glance. Fixed with a scoped override (`.pf-v6-c-masthead .pf-v6-c-button.pf-m-secondary` in `admin.html`) that repoints those same custom properties to `var(--pf-t--global--text--color--inverse)`, matching the masthead's other text/link colors (§24.1's `#tenantLink`/`#headerClock`) instead of fighting PatternFly's button coloring with ad hoc `!important` declarations.

### 26.2 Public Events Page — Header Parity with Admin

**Superseded by §62.** This section's `#headerClock`/`#timezoneModal` text-link-plus-modal pattern (mirroring admin's §24.1 shape) was itself replaced by §62's full redesign of `events.html`. The time-zone control's underlying behavior — a clickable clock opening a time-zone-picker modal, persisting to the shared `samaya_display_tz` `localStorage` key — is carried forward (see §62's `clockBtn`/`timezoneModal`), just restyled and reimplemented in the new `events.css`/`events-public.js` rather than as admin-header-matching PatternFly markup.

### 26.3 Public Events Page — Alliance Switcher

**Superseded by §62.** The `GET /api/alliances` endpoint this section added is unchanged and still backs the public page's alliance list. The `#allianceLink`-plus-modal UI this section built for switching alliances is gone: §62 replaced it with alliance filter chips (click-to-filter toggles in combined view; plain links to other alliances' pages on a single-alliance page) — see §62 for the current mechanism.

### 26.4 Out of Scope

- Making the alliance switcher's list respect any access control — it's public and unauthenticated by nature (matching the pages it links between), so it lists every tenant regardless of who's viewing, same as the combined `/events` page already does.
- Deduplicating the clock/modal implementation between `admin.html` and `events.html` into a shared file — out of scope per §22's established standalone-page convention for the public events page.

## 27. Announcement Templates

**Superseded by the unified event model.** Replaced: event types (§66.1) carry the defaults; the placeholder substitution in `services/templates.py` is reused.

**Replacement planned:** §66 replaces templates with event types. Remains accurate for the running code until that ships.

**Status:** Implemented, pending deployment. §27.1's migration script must run against production before the new application code is deployed, same order as every other schema-changing migration in this repo.

**Problem.** Composing an announcement from scratch every time is repetitive for recurring situations — a daily reset warning, a weekend event reminder — and typing the send time into the message body by hand means it's wrong the moment the reader is in a different time zone, or the announcement gets rescheduled. Two related gaps: no way to save a wording pattern for reuse, and no way to reference "when this is happening" in a way that resolves correctly for every viewer and every recurrence.

**Design:**

- New `AnnouncementTemplate` (table `announcement_templates`): `id`, `owning_tenant_id` (FK, same scoping as `Announcement` itself — a template is managed by whoever can create announcements for that alliance), `name` (unique per tenant), `title_template`, `body_template`, `leadership_only`, `event_offset_minutes` (all default 0/false), `created_at`. Picking a template in the admin UI copies its four content fields into the New Announcement modal — a one-time copy, the same relationship **Duplicate** (§24.2) already has to the announcement it copied from, not a live link back to the template.
- `Announcement` gains `event_offset_minutes` (int, default 0) — independent of templates; a hand-written announcement can use placeholders too, with no template involved at all.
- **Six placeholders**, resolved by `services/templates.py`'s `render_placeholders()`: `{alliance_name}`, `{kingdom_name}`, `{send_time}`, `{send_time_relative}`, `{event_time}`, `{event_time_relative}`. The `event_*` pair is the announcement's own `scheduled_for` shifted by `event_offset_minutes` — the "warn ahead of an event" pattern: a reset warning sent at 23:45 UTC for a 00:00 UTC reset uses `event_offset_minutes: 15` and writes `{event_time_relative}` rather than hand-typing "in 15 minutes" (which goes stale the moment the schedule changes, and reads wrong for a viewer outside UTC). `send_time`/`event_time` render as Discord's own dynamic timestamp markdown (`<t:UNIX:F>`, `<t:UNIX:R>`) — Discord itself renders these client-side, in each viewer's own locale and time zone, with no app code involved once posted; this app has never needed to compute a "your local time" string for a Discord message before, because Discord already does it natively once the raw timestamp is there.
- **Resolution happens per-target, at delivery time** (`scheduler/announcements.py`), not once when the announcement is created or when a template is applied. This is a hard requirement, not a style choice: a single announcement can target multiple alliances (§13.3), each with its own name, so `{alliance_name}` can only be resolved once the specific target being posted to is known; and a recurring announcement's `scheduled_for` advances by `interval_days` on every re-arm (§13.5), so a timestamp resolved once at creation would show the *first* occurrence's time forever instead of each occurrence's own. An announcement's stored `body_markdown` therefore may contain literal, unresolved `{placeholder}` text right up until the moment it's actually sent — this is expected, and is what the admin list shows for an unsent announcement, same as a template's own stored text.
- Unrecognized `{whatever}` text is left untouched rather than raising an error or vanishing (`render_placeholders`' regex substitution only replaces the six known names) — friendlier for a template being drafted incrementally, and safe against Discord markdown ever using brace syntax for something else.
- `Tenant.kingdom` is now `lazy="joined"` (previously default lazy-load), matching `Tenant.server`'s existing eager-loading (§25.2) — needed so `{kingdom_name}` resolves in the scheduler's `session.get(Tenant, ...)` call path without a separate query or a `MissingGreenlet` risk.

### 27.1 Migration Plan

`migrate_add_announcement_templates.py` — idempotent, hand-written (§22's established convention): adds `announcements.event_offset_minutes INTEGER NOT NULL DEFAULT 0` (every existing announcement gets 0, matching its actual pre-migration behavior since no placeholder syntax existed for it to apply to), then creates `announcement_templates` with a `(owning_tenant_id, name)` unique constraint. No rows are seeded — every tenant starts with zero templates. Verified end-to-end against a real Postgres instance (fresh create, idempotent re-run) before this section's status changed to Implemented.

### 27.2 Admin UI Changes

- **Announcements tab**: a new **Templates** table above the existing Announcements table — name, title, event offset, and **Use**/**Edit**/**Delete** actions. **Use** opens the New Announcement modal pre-filled from the template (title/body/leadership/event offset; targets and schedule are always left for the coordinator to fill in fresh, same reasoning §24.2 gives for Duplicate never carrying forward a schedule).
- **New Announcement modal**: gains an **Event Offset (minutes)** field next to Send Date/Time, and its Body field's helper text lists the six placeholders and a short Discord-markdown reminder (bold/italic/underline/strikethrough/spoiler/quote/code-block syntax) inline, rather than a separate reference page — the modal is where someone actually needs it. A new **Save as Template** button captures the modal's current title/body/leadership/event-offset as a new named template (prompted for a name), without scheduling anything — the reverse direction of **Use**.
- **Template modal**: a small dedicated create/edit form (name, title, body, event offset, leadership flag) for managing templates directly, independent of composing an announcement from one.

### 27.3 Out of Scope

- Kingdom-wide or platform-wide shared templates — a template stays scoped to the tenant that created it, same as Announcements themselves; two alliances wanting the identical "Daily Reset Warning" text each create their own copy. Revisiting this is a natural companion to §25's `DiscordServer` sharing model if it comes up again, but isn't part of this change.
- A live preview of the rendered message (showing what `{event_time}` will actually look like) before saving — the admin list already shows the literal template text, same as it always has for a `body_markdown` field; a true preview would need to fabricate a fake target/kingdom, which risks being misleading rather than clarifying. (Built in §28 — the composer's live preview uses the modal's own real target rows instead of a fabricated one, which removed this objection.)
- Placeholders in Event *names* or Announcement *titles* — both remain literal text with no substitution; titles/names are admin-list-only and never posted to Discord, so there's no viewer-facing reason to resolve placeholders in them. (Event *descriptions* and the pre-event Discord ping's own extra line did gain the same six placeholders, in §32 — only names/titles are still out of scope.)
- Any new markdown *capability* beyond what Discord already supports — this section documents existing Discord markdown for the admin's benefit and adds timestamp placeholders, but doesn't add formatting Discord itself can't render.

## 28. Announcement Composer — Markdown Toolbar, Live Preview, and Emoji Picker

**Status:** Implemented, pending deployment. Client-side only — no migration, no new backend tables.

**Problem.** §27 gave the admin markdown syntax and six placeholders to work with, but composing a body was still typed blind: the coordinator had to already know Discord's markdown syntax by hand, and had no way to see what `{event_time_relative}` or a role mention would actually look like before sending. §27.3 explicitly deferred a live preview, on the reasoning that faking a target/kingdom risked being more misleading than helpful — this section resolves that by using the modal's own live target rows as the preview's data source instead of fabricating anything, which removes the original objection.

**Design:**

- **Markdown toolbar.** A row of buttons above both the announcement Body field (`#aBody`) and the Template Body field (`#tBody`): Bold, Italic, Underline, Strikethrough, Spoiler, Quote, Inline Code, Code Block. Each wraps the textarea's current selection with the matching Discord markdown syntax (`**`, `*`, `__`, `~~`, `||`, `` ` ``, `` ``` `` on their own lines) via a shared `wrapSelection(textareaId, before, after)` helper in `static/js/announcements.js`; Quote instead prefixes every selected line with `> ` via `prefixSelectedLines()`, since it's a line-level marker rather than a wrap. With no selection, the wrap markers are inserted at the cursor with placeholder text selected between them (e.g. bolding nothing selects the word "bold" between the `**`s) so the coordinator can type over it immediately. Every toolbar action re-focuses the textarea and fires its `input` event, so the char counter (announcement body only) and the live preview (below) update immediately.
- **Live preview.** A preview pane below each Body field, styled as an approximate dark-theme Discord message bubble (bot avatar circle, "Samaya" name with a "BOT" tag, and the rendered message), updating on every keystroke. It resolves the same six §27 placeholders client-side (`clientRenderPlaceholders()` in `announcements.js`, a small JS port of `services/templates.py`'s `render_placeholders()` — kept as a second implementation rather than shared code, since one runs in the browser and one on the server and this app has no shared-code mechanism between them per §22) using:
  - **Alliance/kingdom name** — from a small **Preview As** dropdown next to the pane, populated from the modal's own target rows' tenant selections (so it only ever shows alliances actually targeted, never a fabricated one) and re-populated whenever a target row is added, removed, or changed. The template modal has no targets, so its preview always uses the current tenant (`getCurrentTenantSlug()`).
  - **Send time** — the Send Date/Time fields if both are filled; otherwise the current time, with a small note ("using current time — no send time set yet") so the coordinator isn't misled into thinking a real schedule was read.
  - **Event time** — Send time shifted by the Event Offset field, same as the server does.

  Unlike the real Discord message (which uses `<t:UNIX:F>`/`<t:UNIX:R>` and lets each viewer's own Discord client render it in their own locale), the preview computes a human-readable approximation once, in the *admin's own browser locale and time zone* (`formatDiscordAbsolutePreview()`/`formatDiscordRelativePreview()`) — there is no way to show "every viewer's own time zone" in a single static preview, so it shows one representative rendering and is labeled "(preview)" next to each resolved timestamp to make clear it's an approximation, not the literal Discord output.

  The resolved text is then run through a small markdown-to-HTML renderer (`renderDiscordMarkdownPreview()`) covering exactly the syntax §27 already documented as supported — bold/italic/underline/strikethrough/spoiler/inline-code/code-block/quote/headers — plus two Discord mention forms:
  - `<@&ROLE_ID>` is resolved to the real role name (`@RoleName`, styled as a blue mention pill) by fetching `GET /api/discord/roles` (existing endpoint, §15's Config tab already uses it) for the **Preview As** tenant, cached per tenant slug for the life of the modal session so re-renders on every keystroke don't refetch.
  - `<#CHANNEL_ID>` is resolved the same way against `GET /api/discord/channels` (also existing, already fetched per target row) if that channel ID happens to be one of the currently-loaded target rows' channel lists; otherwise it falls back to a generic `#channel` pill, since resolving an arbitrary typed-in channel ID would mean fetching every channel for every render.
  - A spoiler (`||text||`) renders as a black bar that reveals its text on click, matching Discord's own client behavior, so the coordinator can verify spoiler placement without it always being visible while editing.
- **Emoji picker.** A 🙂 button next to each toolbar inserts Discord's default (standard Unicode) emoji — not a server's custom/uploaded emoji, which would need per-guild fetching and image assets. Clicking it opens a small popover grid of ~80 commonly-used Unicode emoji (curated in a static `EMOJI_PICKER_LIST` in `common.js`, shared by both textareas), grouped under a few informal headings (Faces, Gestures, Symbols). Clicking an emoji inserts it at the textarea's current cursor position (`insertAtCursor()`), leaves the picker open for inserting more than one, and closes on an outside click. No new dependency or image assets — browsers render standard Unicode emoji glyphs natively, the same glyphs Discord's own client shows for non-custom emoji.

### 28.1 Out of Scope

- Resolving `<@&ROLE_ID>`/`<#CHANNEL_ID>` mentions server-side, anywhere — this remains an admin-preview-only convenience; the literal Discord markup is still what's stored and sent, and Discord's own client does the real resolution for the actual reader, same as before this section.
- A picker or preview for a server's custom (uploaded) emoji — deliberately scoped to Discord's default Unicode set only, per the user's request; custom emoji would need a per-guild fetch and image rendering this app has no other use for.
- Making the preview byte-for-byte identical to Discord's actual rendered output — it's an approximation for drafting purposes (most visible in the timestamp placeholders, which Discord renders in each *viewer's* own locale/zone, not the admin's), not a pixel-accurate Discord client reimplementation.
- Any preview or toolbar support in the pre-event Discord ping's own fixed surrounding text (`occurrences.py`'s `announce_msg`) — only the Event's own `description` field (and Announcement/Template bodies) are user-authored text; the ping's generated structure around it is not. (The Event *description* field did gain the full composer — toolbar, live preview, emoji picker — in §34; this bullet now covers only the ping's own fixed text.)

## 29. Coordinator Quality-of-Life Improvements

**Status:** Implemented, pending deployment. Client-side only — no migration, no new backend surface.

**Problem.** A set of small, repeated frictions in day-to-day coordinator use: picking the same alliance/channel combination by hand on every announcement, scrolling a growing Events or Post Log table with no way to narrow it, a Dashboard that goes blank by evening even when something's happening early the next morning, and occurrence times shown only as absolute UTC with no sense of "how soon."

**Design:**

- **Remembered last-used Discord channel per tenant.** A brand-new target row — on both the Event modal (`addEventTargetRow`/`populateEventTargetFields` in `events.js`) and the Announcement modal (`addAnnouncementTargetRow`/`populateAnnouncementTargetChannels` in `announcements.js`) — now defaults its channel dropdown to whatever channel was last picked for that tenant, via `getLastChannelForTenant()`/`setLastChannelForTenant()` in `common.js` (a `{tenantSlug: channelId}` map in `localStorage`, per-browser, never sent to the server). This only ever supplies a *default* for a row with no explicit channel — editing or duplicating an existing event/announcement still shows its real stored channel, untouched, since the caller only falls back to the remembered value when no `channelId` was passed in at all.
- **"+ Add all alliances"** button next to "+ Add another target" in the Announcement modal (`addAllAllianceTargets()` in `announcements.js`) — adds one target row per tenant not already present among the modal's current target rows, each defaulting to that alliance's own last-used channel (or "— none —" if it's never been posted to). Turns the common "post this to every alliance" case from one click-through per alliance into one click total.
- **Filter box on the Events and Post Log tables.** Both views now cache their last-fetched list (`EVENTS_CACHE`, `POSTLOG_CACHE`) and re-render through a small client-side text filter (`filterEventsTable()` matching event name or alliance name; `filterPostLogTable()` matching event name, poster, or status) rather than re-fetching — the same "cache the list, filter/re-render from it" pattern `ANNOUNCEMENTS`/`ANNOUNCEMENT_TEMPLATES` already use.
- **Dashboard "Upcoming (Next 48h)" section**, alongside the existing "Today's Events" — using the same already-fetched `GET /api/occurrences` data, filtered to occurrences starting within the next 48 hours whose date isn't today (so nothing appears twice between the two sections). Catches the coordinator checking in the evening before an early-morning event, when "Today" would otherwise be empty.
- **Relative-time badges** ("in 20 minutes", "3 hours ago") on Schedule table rows and both Dashboard sections, via a new shared `formatRelativeTime(date)` in `common.js` — the same threshold logic the announcement composer's preview (§28) already computed, now factored out so it has one implementation instead of two. The composer's own `formatDiscordRelativePreview()` becomes a thin wrapper adding its "(preview)" qualifier.

### 29.1 Out of Scope

- A full mobile-responsive layout pass on the admin UI — a real gap (PatternFly's table component doesn't collapse gracefully on a phone), but a layout project in its own right, not a quality-of-life patch.
- Server-side search/pagination for Events or Post Log — both filters are client-side over the already-fetched list; if either list grows large enough that fetching it all becomes slow, that's a separate, larger change (paginated endpoints), not part of this section.
- Remembering anything beyond the last-used channel (e.g. a remembered role, or per-user rather than per-browser preferences) — scoped to exactly the one repeated pick this section identified as friction.

## 30. Failed-Target Retry, CI, and Automated Backups

**Status:** Implemented, pending deployment. The application-code half (retry) needs no migration; CI and the backup script are operational additions, not app behavior — CI takes effect the moment `.github/workflows/ci.yml` lands on `master`, and the backup script needs a one-time `crontab` install on `lxc-taraka` (§30.2) that this patch cannot do by itself.

**Problem.** Three related operational gaps, each identified while reviewing the roadmap before this deploy: a one-time announcement that ends with some targets in `error` never gets reconsidered by anything — the coordinator's only recourse was re-creating the announcement from scratch; `pytest` only ran when it happened to be run locally before handing off a patch, so nothing caught a regression automatically between that and the manual production deploy; and Postgres backups only ever happened by hand, immediately before a schema migration — there was no routine backup a bad day (disk failure, an unrelated bug) could fall back on.

### 30.1 Retry Failed Announcement Targets

- New endpoint **`POST /admin/api/announcements/{id}/retry-failed-targets`** (`routers/admin/announcements.py`): resets every `AnnouncementTarget` on that announcement currently in `post_status == "error"` back to `"pending"` (clearing `status_detail` and `discord_message_id`), and — the part that actually matters — sets the announcement's own `status` back to `"scheduled"`, since `scheduler/announcements.py`'s delivery tick only ever looks at announcements with `status == "scheduled"`. `scheduled_for` is left untouched (it's already in the past, since the announcement already fired once), so the very next per-minute tick picks the retried target(s) back up. 400s with "No failed targets to retry" if there are none; 404s if the announcement doesn't exist or isn't owned by the calling tenant, same ownership check `cancel_announcement`/`delete_announcement` already use.
- This only matters for a **one-time** announcement. A *recurring* announcement already self-heals on its own next re-arm cycle — `send_scheduled_announcements` resets every target (including ones sitting in `error`) back to `pending` when it advances `scheduled_for` for the next occurrence (§13.5) — so this endpoint's real audience is a one-time announcement that went terminal (`failed`, or `posted` with some targets still in `error`) with nothing left to ever look at it again.
- **Admin UI**: a **Retry Failed** button appears in the Announcements table's Actions column whenever at least one target is in `error` and the announcement isn't `cancelled` (retrying a cancelled announcement would effectively un-cancel it, which isn't the intent) — `retryFailedTargets()` in `announcements.js`, calling the endpoint and reloading the list.

### 30.2 Continuous Integration

- New **`.github/workflows/ci.yml`**, two jobs on every push and pull request against `master`: `pytest` (installs `app/requirements-dev.txt`, runs the full suite — no Postgres service needed, since `tests/conftest.py` already uses an in-memory SQLite database) and `eslint` (`npm ci` + `npm run lint`, the same lint this repo's `package.json`/`eslint.config.mjs` already define and this session already ran by hand before every patch). This doesn't replace the existing patch-verification practice (commit, diff, clean-apply-in-a-worktree, full suite) for work delivered through this conversation — it's a second, automatic check once a patch actually lands on `master`, catching anything that practice might have missed.

### 30.3 Automated Nightly Backups

- New **`ops/backup.sh`**: runs `docker compose exec db pg_dump ...` (the exact command the deployment runbook already has the operator run by hand before a schema migration), gzips the output to `/opt/taraka/backups/kingshot_scheduler_<timestamp>.sql.gz`, and rotates anything older than 14 days. Runs on the **host** (the LXC), not inside the `app` container — `app`'s image (`python:3.13-slim`) has no `pg_dump`, and the `db` container (`postgres:16-alpine`) already has one and is already reachable via `docker compose exec`, so there's no reason to add a new dependency to the app image for this.
- **Requires a one-time manual install step during deployment** — this patch can land the script, but only the operator, on the actual host, can add the crontab entry: `chmod +x /opt/taraka/ops/backup.sh` then a `crontab -e` entry (`0 3 * * * /opt/taraka/ops/backup.sh >> /opt/taraka/backups/backup.log 2>&1`, full instructions in the script's own header comment). This is exactly the kind of decision a person has to make on their own machine, not something a patch applies for them.

### 30.4 Out of Scope

- Restore tooling/runbook — this section covers taking backups, not the (hopefully never-needed) restore procedure. Worth its own pass if it ever comes up for real, so the first time someone reads a restore runbook isn't during an actual incident.
- Off-box/offsite copies of the backup — `ops/backup.sh` only writes to local disk on the same LXC as the database, which doesn't protect against that LXC's own disk or host failing entirely. Adding an rclone/rsync/restic step to copy dumps elsewhere is a natural follow-up, deliberately left out here since it depends on where the operator wants them to land (already documented as a next step in the script's own comments).
- Automatic retry beyond the coordinator clicking the button — no background job re-attempts a failed target on its own; §30.1 makes retrying possible and visible, not automatic. An unattended auto-retry risks masking a real, recurring problem (a revoked bot token, a deleted channel) behind repeated silent retries instead of surfacing it.
- A CI gate that blocks merging on failure (branch protection rules) — that's a GitHub repository setting, not something this patch can configure from a file in the repo; the workflow runs and reports either way, and turning failures into a hard block is a one-click follow-up in the repo's own settings if wanted.

## 31. Audit Log Viewer, Delivery Health Card, and Read-Only Viewer Role

**Status:** Implemented, pending deployment. §31.3's migration script must run against production before the new application code is deployed, same order as every other schema-changing migration in this repo.

**Problem.** Three related medium-value roadmap items, bundled into one patch since two of them (audit log, viewer role) touch the same access-control surface: `services/audit.log_change` has written to `audit_log` from six router call sites since early in this app's life, but nothing ever reads it back — an owner has no way to actually see who changed what. Delivery success/failure for both Announcements and event Discord posts is visible only by opening the Announcements list or Post Log and reading per-row status — there's no at-a-glance rollup for "is delivery generally healthy right now." And every tenant grant is either full read/write access (`coordinator`) or full access plus member management (`owner`) — there was no way to give someone (an officer who wants visibility, a departing coordinator being wound down gently) access to look without being able to change anything.

### 31.1 Audit Log Viewer

- New **`GET /admin/api/audit-log`** (`routers/admin/audit_log.py`), owner-only (`require_tenant_owner` — same tier as `/api/members`, since an audit trail of who-changed-what is an owner-level concern by the same reasoning member management already is). Tenant-scoped by `AuditLog.tenant_id`; a superadmin-only global action logged with `tenant_id=None` (e.g. removing a kingdom-coordinator grant) is invisible here by design — this endpoint has no "platform-wide" mode, matching every other tenant-scoped list in this app. Returns each row's timestamp, the acting user's Discord username (resolved via an outer join — a row with `user_id=None`, if one is ever written, shows `null` rather than 404ing or omitting the row), table name, row id, action, and the `before`/`after` JSON blobs parsed back into objects (they're stored as serialized text per `AuditLog`'s own docstring — §4).
- No write/delete surface — `audit_log` was already documented as append-only and explicitly not a rollback mechanism (§22); this section only adds a way to read it, nothing that could mutate it.
- **Admin UI**: a new **Audit Log** tab, listing entries newest-first with a compact per-row diff (only the fields that actually changed between `before`/`after`, not the full raw JSON — a real diff viewer is more machinery than a first version needs). Its UI visibility was owner-only at the time this was written; §38.6 later made it superadmin-only (the backend endpoint itself is still `require_tenant_owner`-gated, unchanged).

### 31.2 Delivery Health Card

- New **`GET /admin/api/delivery-health`** (`routers/admin/status.py`, alongside the existing `/api/status`), current-tenant-scoped like everything else in that file. Aggregates two independent counts over a trailing 7-day window: `AnnouncementTarget.post_status` for this tenant's announcement deliveries (windowed by the parent `Announcement.posted_at`, since the per-target row itself carries no timestamp), and `PostLog.status` for this tenant's event Discord posts (windowed by its own `posted_at_utc`). Returns `{window_days, announcements: {posted, error}, event_posts: {posted, error, cancelled}}`.
- **Admin UI**: a **Delivery Health** card on the Dashboard, below the existing Upcoming (§29) section — one row per channel (Announcements, Event Posts) showing a posted-count badge, an error-count badge when nonzero, and a success-rate percentage, or "No activity in the last 7 days" when there's nothing to rate. This is deliberately a rollup, not a drill-down — a coordinator who sees failures here still goes to Announcements or Post Log to act on the specific one, same as today.

### 31.3 Read-Only Viewer Role

- `user_tenants.role` and `invites.role` both gain `viewer` as a third valid value (`ck_user_tenant_role`/`ck_invite_role` CHECK constraints widened accordingly). Migration: `migrate_add_viewer_role.py` — idempotent (reads each constraint's current definition via `pg_get_constraintdef` and only widens it if `'viewer'` isn't already present), verified end-to-end against a real Postgres instance (fresh pre-migration schema, migrate, re-run to confirm no-op, confirmed the widened constraint still rejects a bogus role value) before this section's status changed to Implemented.
- New dependency **`require_not_viewer`** (`routers/admin/deps.py`) — the mirror image of `require_tenant_owner`: superadmin bypasses, otherwise a `viewer` grant on the current tenant 403s ("Viewer access is read-only") and any other role (or no explicit grant row, which shouldn't reach this dependency at all since `get_current_tenant` already gates that) passes through. Unlike `require_tenant_owner` this isn't a match-required lookup (`owner` and `coordinator` both pass; only `viewer` is rejected), so it's a rejection check rather than a role-equality check.
- Swapped onto every mutating (POST/PATCH/PUT/DELETE) endpoint that previously depended on plain `get_current_tenant`: all of `events.py`, `announcements.py` (including §30.1's `retry-failed-targets`), `announcement_templates.py`, `occurrences.py`'s three write endpoints (`update_occurrence`, `post_occurrence`, `cancel_occurrence_discord` — the first two took no tenant dependency at all before this and gained one purely for this check, via a plain `_: Tenant = Depends(require_not_viewer)`), all four of `discord_sync.py`'s POST endpoints, and `scheduler_control.py`'s `manual_regenerate`. Every GET/list endpoint in these routers is untouched — a viewer still passes `get_current_tenant`'s plain access check on those, same as a coordinator would. `invites.py`'s member-management endpoints were already `require_tenant_owner`-gated (stricter than `require_not_viewer`, since a viewer isn't an owner either), and `tenants.py`'s Kingdom/Tenant/DiscordServer CRUD is already superadmin-only — neither needed changes.
- **Admin UI**: the Access tab's **Change role** action becomes a three-way `prompt()` (`"owner"`/`"coordinator"`/`"viewer"`, pre-filled with the person's current role) in place of the old blind owner↔coordinator toggle, and the invite-creation prompt's validation accepts `"viewer"` alongside the existing two values (`access.js`). No change was needed in `routers/auth.py`'s invite-claim flow — it already passes `invite.role` through generically to the new `UserTenant`/existing grant row, special-casing only `kingdom_coordinator`.
- A `viewer` still counts toward `_sole_owner()`'s denominator the same as any non-owner grant does (i.e., not at all) — demoting or removing the only remaining `owner` is still blocked regardless of what role they'd become, unchanged from §21.1.

### 31.4 Out of Scope

- Exporting the audit log (CSV, like Post Log's export) — the in-app viewer covers the stated need; add an export if it comes up for real.
- Any retention/pruning policy for `audit_log` — it grows unbounded today and this section doesn't change that; a real gap if the table ever gets large enough to matter, but not part of this patch.
- A finer-grained Delivery Health breakdown (per-alliance in combined mode, per-event-type, a trend chart over time) — the single current-tenant 7-day rollup answers "is anything on fire right now," which is what was asked for; a historical/trend view is a larger, separate feature.
- Letting a `viewer` see the Access & Platform, Audit Log, Config, or Sync tabs — `applyRoleVisibility()` still hides these from a `viewer` (Access and Audit Log became superadmin-only rather than owner-only per §38.6, but a viewer was never in either tier); a viewer's read access is scoped to the same views a coordinator already sees (Dashboard, Events, Announcements, Schedule, Post Log), just without any of that content's write actions available.
- A `viewer`-specific UI treatment (e.g. hiding Create/Edit/Delete buttons entirely rather than letting them 403) — out of scope for this pass; a viewer clicking a write action today gets the same 403 toast any permission failure produces, not a dedicated "you're read-only" affordance. Worth a follow-up UI pass if viewers turn out to be a commonly-used role in practice.

## 32. Bulk Event Import/Export, and Placeholder Support in the Pre-Event Ping

**Superseded by the unified event model.** The CSV import and export was not carried over (§66.11). Placeholder support lives on in `services/templates.py`.

**Status:** Implemented, pending deployment. No migration — both halves are additive endpoints/behavior on existing tables.

**Problem.** Two small, unrelated roadmap items bundled into one patch since both are the last of this round's build list. First: setting up a season's worth of recurring events one at a time through the modal is repetitive busywork for anything beyond a handful of events, and there was no way to get an alliance's events *out* of the system either (to hand to another alliance standing up the same schedule, or just to back up outside the app). Second: §27 gave Announcements six `{placeholder}`s (alliance/kingdom name, send/event time and their relative forms) but scoped them to Announcement bodies only (§27.3) — an Event's own `description`, which reaches Discord in two places (the Scheduled Event's own description field, and now the pre-event ping), stayed literal text with no way to reference "when this actually starts" without hand-typing a UTC time that goes stale the moment the schedule changes.

### 32.1 Bulk Event Import and Export

- **New `GET /admin/api/events/export.csv`** (`routers/admin/events.py`) and **`POST /admin/api/events/import.csv`**, a matched pair per the user's explicit request that import be "bundled with" export rather than shipped alone. Both use one shared column set (`_BULK_EVENT_COLUMNS`): `name, interval_days, start_time_utc, duration_hours, discord_channel, description, leadership_only, anchor_date, notification_channel_id, notification_role_id, notify_minutes_before` — deliberately excludes `scope` and `targets`. A kingdom-wide event's automatic same-Kingdom fan-out and an explicit `EventTarget` list (§20) are both multi-row, cross-tenant concepts that don't flatten into one CSV row per event without either lying about what a plain re-import would reconstruct or inventing a second, more complex file format nobody asked for — so bulk import/export is scoped to **alliance-scope events with no explicit targets**, full stop. Anything more exotic still goes through the modal, one at a time, same as today.
- **Export** (`GET`, `get_current_tenant`-scoped, read-only) selects the current tenant's own `scope == "alliance"` events, ordered by name, and streams them as CSV via the same `StreamingResponse` pattern `post_log.py`'s existing export already uses.
- **Import** (`POST`, `require_not_viewer`-gated since it's a write, multipart file upload via a new `UploadFile` parameter — this repo's first, so `python-multipart` is now a runtime dependency) parses the uploaded file with `csv.DictReader`, 422s up front if any required column is missing, and otherwise validates **each row independently** by constructing an `EventIn` (reusing every validator the single-event create form already enforces, rather than a second copy of the same rules) — a bad row doesn't abort the whole file. Every event is created with `scope` forced to `"alliance"` regardless of what the CSV might claim (there's no `scope` column at all, so this is moot in practice, but the endpoint doesn't trust row content it didn't ask for). The response is `{created: N, errors: [{row, detail}]}`, `row` being the CSV's own 1-indexed row number (header = row 1, so the first data row is row 2 — matching what a coordinator sees opening the file in a spreadsheet) so a partially-bad file can be fixed and re-uploaded without guessing which lines failed.
- **Import is additive only, never an upsert** — re-importing the same file twice creates duplicate events by design, matching how every other create endpoint in this app behaves (there's no existing merge-by-name convention anywhere to be consistent with, and inventing one here risks silently overwriting an event someone's since edited by hand).
- Each successfully-created row is logged individually via `log_change` with `after: {..., "source": "bulk_import"}`, so the Audit Log (§31.1) can distinguish a bulk-imported event from one created through the modal.
- **Admin UI**: **Export CSV** and **Import CSV** buttons next to "+ Add Event" (`exportEventsCsv()`/`importEventsCsv()` in `events.js`) — export follows the same fetch-blob-download pattern `postlog.js`'s existing CSV export already uses; import is a hidden `<input type="file">` triggered by the visible button, posting the chosen file as `FormData` (outside the `api()` helper, which always sends JSON) and reporting `{created, errors}` back as a toast, with the full per-row error list also logged to the browser console for a bulk failure too large to fit in a toast.
- **Round-trip guarantee**: export's column order and format is exactly what import expects, so "export, tweak a few rows in a spreadsheet, re-import" works with no reshaping — the property this section is actually valuable for (e.g. copying one alliance's event lineup to a new alliance being onboarded), rather than "each direction happens to exist."

### 32.2 Placeholder Support in the Pre-Event Ping and Event Description

- `Event.description` now has the same six `{placeholder}` names §27 built for Announcements (`{alliance_name}`, `{kingdom_name}`, `{send_time}`, `{send_time_relative}`, `{event_time}`, `{event_time_relative}`) resolved through it, via the same `services/templates.render_placeholders()` — no second implementation. Resolution happens once per post attempt in `_post_to_one_tenant` (`routers/admin/occurrences.py`), before either place `description` reaches Discord, so both destinations see the identical resolved text:
  - The Discord **Scheduled Event's own description field** (`create_discord_event(..., description=resolved_description)`), previously the raw literal text.
  - A new extra line in the **pre-event ping** itself (`announce_msg`), which previously carried no user-authored text at all — just the event name, date/time, channel, and a "click Interested" footer. A coordinator can now write something like `"Bring siege gear! Starts {event_time_relative}"` in the description and have it land directly in the role-pinged channel message, not just buried in the Scheduled Event's own detail view.
  - For a kingdom-wide event's fan-out, resolution happens **once per target tenant** (same loop `_post_to_one_tenant` already runs per target) so `{alliance_name}` still means the alliance actually being posted to, matching how Announcements' per-target resolution already works (§27's "resolution happens per-target, at delivery time" is unchanged in spirit — this just extends which delivery path it applies to).
- **What `{send_time}`/`{event_time}` mean here, since an Event (unlike an Announcement) has no independently-scheduled "send time" of its own**: `send_time`/`send_time_relative` resolve to *now* (the actual moment this post/ping is going out), and `event_time`/`event_time_relative` resolve to `occ.start_datetime_utc` — computed by passing `render_placeholders()` an `event_offset_minutes` equal to the live minutes between now and the occurrence's own start, rather than a stored offset field the way Announcements use one. In practice `{event_time_relative}` is the placeholder actually worth using here (e.g. "Siege begins {event_time_relative}"); `{send_time}` exists mainly for symmetry with the Announcement placeholder set rather than because posting time is something worth calling out in an event's own description.
- Unrecognized `{whatever}` text is left untouched, same as §27 — an event description with no placeholders in it at all (the overwhelming majority, retroactively) is unaffected byte-for-byte.
- No UI change beyond documentation: the Event modal's Description field gains the same short placeholder-list helper text the Announcement Body field already has (§27.2), so a coordinator discovers the feature without needing to read this spec.

### 32.3 Out of Scope

- Bulk import/export for kingdom-wide events or events carrying explicit `EventTarget` rows — per §32.1, these don't flatten into one CSV row without either lying about the reconstruction or a second, more complex file format; use the modal for these.
- Bulk *editing* (importing a CSV that updates existing events by name/id rather than only creating new ones) — import is additive-only by design; an upsert mode is a natural but materially different follow-up, not requested here.
- A preview/dry-run step before committing an import (uploading a file and seeing what *would* happen before it actually creates anything) — the per-row error reporting already tells a coordinator what failed after the fact; a true dry-run is more machinery than this version needs, and a bad import can already be cleaned up row-by-row via the existing Delete action.
- Placeholders in Event **names** or Announcement **titles** — unchanged from §27.3's existing scope boundary; both remain literal, since titles/names are typically short identifiers shown in admin lists, not viewer-facing prose.
- A dry-run or live preview of the resolved Event description before saving (mirroring §27.3's explicitly-deferred Announcement preview, later actually built in §28 once target rows existed to preview against) — an Event has no per-post "preview as" context the way a multi-target Announcement composer does, and this section's scope was placeholders working correctly at send time, not a composer rebuild for Events.

## 33. Post-Deployment Bug Fixes: Template Use, Target Channel Validation, Cross-Server Channel Errors

**Status:** Implemented, pending deployment. Client-side fixes plus one server-side error-message improvement — no migration.

**Problem.** Three issues reported after §27–§32 went live:

1. **The Templates table's "Use" button did nothing.** `useTemplate()` (`announcements.js`) opens the New Announcement modal pre-filled from a template-shaped object carrying only `{title, body_markdown, leadership_only, event_offset_minutes}` — a template has no `targets` array at all, since targets are deliberately never copied from a template (§27.2). `openAnnouncementModal()` unconditionally read `source.targets.length` before checking `source.targets` was defined, so every call from `useTemplate()` threw a `TypeError` and the modal never opened — silently, since the throw happened before `classList.add('open')` ran and nothing else was listening for it. `duplicateAnnouncement()`'s source objects (real Announcements) always have a `targets` array, which is why Duplicate worked fine and only Use was affected.
2. **A new announcement's target row can silently fail to save.** A brand-new target row defaults to that tenant's last-used Discord channel (§29) or, the first time ever posting to a tenant, to a "— none —" placeholder option — visually indistinguishable from a real selection. Saving with that row untouched correctly refused with a toast ("Every target needs a Discord channel selected"), but nothing about the row itself indicated *which* target was the problem, and a transient toast is easy to miss while looking at the form fields — this is what read as the announcement "not saving" for no visible reason.
3. **Channel-list load failures for a target tenant gave no indication of the actual cause.** Selecting a target tenant whose Discord channels fail to load (e.g. `GET /api/discord/channels` 403s because the caller lacks `UserTenant` access to that tenant, or 502s because that tenant's own Discord server/bot rejected the call) fell back to a manual channel-ID text field with a generic "Could not load Discord channels for X" toast — the same message for two failure modes that need entirely different fixes (get access granted to that tenant, vs. fix that tenant's Discord server/bot configuration), which is exactly the ambiguity behind the "can't see another alliance's channels" report. Note that seeing another tenant's channels in this picker was already, and remains, gated on holding `UserTenant` access to that tenant (`get_current_tenant`'s own access check, shared by every per-tenant Discord call) — that access boundary is intentional (§13.3: "a user can only ever pick tenants they hold UserTenant access to") and this section does not change it; it only makes it visible when that's the actual reason a channel list didn't load.

**Fixes:**

- `openAnnouncementModal()`'s targets check is now `source && source.targets && source.targets.length` — a template source (or any object with no `targets` key at all) correctly falls through to the fresh-row default, same as calling the modal with no source.
- `saveAnnouncement()` now marks every announcement-target-row missing a channel with a `target-row-invalid` CSS class (a red outline on the channel select/fallback input) before showing the validation toast, and scrolls the first invalid row into view. The class clears the moment that row's channel is actually picked (or typed into the fallback field), so the highlight tracks the live state of the form rather than freezing at the moment Save was clicked.
- `populateAnnouncementTargetChannels()`'s failure toast now includes the server's actual error message (`e.message`, already available from `api()`'s thrown `Error` — previously discarded) alongside the tenant slug, so a 403 ("You don't have access to this tenant") and a 502 (a Discord API failure from that tenant's own bot/server config) read as the two different problems they are, instead of one generic "could not load" line.

### 33.1 Out of Scope

- Changing which tenants a coordinator can target or preview channels for — the `UserTenant`-access boundary (§13.3) is unchanged; this section only makes a failure inside that boundary diagnosable instead of silent.
- Auto-selecting a real channel instead of defaulting to "— none —" on a tenant's first-ever target row — §29's documented behavior for that case is unchanged; the fix here is making the empty state visibly wrong when saved, not picking a channel on the coordinator's behalf.

## 34. Composer Enhancements: Headings, Role Mention Picker, Event Description Composer

**Status:** Implemented, pending deployment. Client-side only — no migration, no new backend tables.

**Problem.** Three related composer gaps surfaced once §28's toolbar/preview and §32's Event-description placeholders had both been in use for a while:

1. **No way to insert a heading.** `renderDiscordMarkdownPreview()` has always resolved `#`/`##`/`###` lines to `<h1>`/`<h2>`/`<h3>` (§28's own text lists "headers" among the syntax it covers), but the toolbar itself — Bold/Italic/Underline/Strikethrough/Spoiler/Quote/Inline code/Code block — never grew a matching button, so a coordinator had no way to discover the feature short of already knowing Discord supports `# ` headers and typing it by hand. Separately, the header regexes anchored on `$` rather than consuming the line's own trailing newline, so the preview's "remaining newlines become `<br>`" pass added an extra line break after every heading on top of the `<h1>`/`<h2>`/`<h3>` tag's own block margin — a real (if minor) rendering glitch, fixed alongside the missing button since both are in the same function.
2. **Typing a role mention by hand means looking up a numeric Discord role ID** — exactly the kind of manual, error-prone step the rest of the composer (channel/role *preview* resolution, §28) exists to reduce, but nothing let a coordinator actually *insert* a correct mention; the preview would only render one correctly after the fact.
3. **Event descriptions had none of §28's composer** (toolbar, live preview, emoji picker) — an explicit exclusion at the time (§28.1: "Any preview or toolbar support in ... Event name/description fields"), reasoned as out of scope because "an Event has no per-post 'preview as' context the way a multi-target Announcement composer does" (§32.3). That reasoning no longer holds now that §32 gave Event descriptions the same six placeholders Announcements have, resolved against a single owning tenant (or each fan-out target tenant in turn) — exactly the "preview as one tenant" shape the Template modal's composer (§28) already handles, not the multi-target shape that motivated the original exclusion.

**Design:**

- **Heading button.** A **H** button in every markdown toolbar (Announcement Body, Template Body, and now Event Description — see below), next to Quote since both are line-level prefixes: `prefixSelectedLines(textareaId, '# ')`, reusing the exact helper Quote already uses. `renderDiscordMarkdownPreview()`'s three header regexes now consume their line's trailing `\n` (`/^# (.*)\n?/gm` in place of `/^# (.*)$/gm`, same change for `##`/`###`) instead of leaving it to become a spurious `<br>`. The Body helper text's inline markdown reminder now lists `# heading` alongside the existing bold/italic/etc. examples.
- **Insert role mention.** A small **"Insert role mention…"** `<select>` next to the emoji picker button in every toolbar (`aRoleMention`/`tRoleMention`/`mRoleMention`), populated with `@RoleName` options for whichever tenant the composer's own **Preview As** selector currently points at — refreshed by `refreshRoleMentionSelect()` inside `renderComposerPreview()` itself, reusing the exact `ensurePreviewRolesLoaded(tenantSlug)` call (and its per-tenant cache) the live preview's own mention-resolution already makes, so this adds zero new API calls. Picking a role calls `insertAtCursor(textareaId, '<@&' + roleId + '>')` — the raw Discord mention syntax, immediately visible in the live preview as a resolved `@RoleName` pill (§28's existing mention rendering) — and resets to the placeholder option so it reads as a one-shot insert action, not a persistent field.
- **Event Description composer.** The Event modal's Description field gains the identical toolbar (now including Heading and Insert-role-mention), a live preview pane, and the emoji picker — all wired through the same `renderComposerPreview()` helper Announcements/Templates already share, via a new `renderEventDescriptionPreview()` entry point and a new **Preview As** selector (`mPreviewTenant`, populated from the current tenant only — an Event modal has no multi-target row list the way the Announcement modal does, matching how the Template modal's own preview already defaults to the current tenant). The one real difference from the Announcement/Template preview: an Event has no independently-scheduled "send time" of its own (§32.2 already established this for the actual send path) — the composer's `renderComposerPreview()` now accepts an `eventDateId`/`eventTimeId` pair (wired to the modal's existing **Anchor Date** and **Start Time** fields) as an alternative to the Announcement/Template's `dateId`/`timeId` + `offsetId`: in this mode, `{send_time}` always previews as *now*, and the offset between *now* and the Anchor Date/Start Time combination becomes the `eventOffsetMinutes` fed into the exact same `clientRenderPlaceholders()` math every other preview already uses for `{event_time}` — no second placeholder-resolution implementation, just a different way of arriving at the same two inputs (`scheduledFor`, `eventOffsetMinutes`) the shared function already takes. With no Anchor Date/Start Time set yet, the preview falls back to "now" for both and shows a note to that effect, same pattern as the Announcement composer's own "no send time set yet" fallback.

### 34.1 Out of Scope

- Custom (server-uploaded) emoji or role/channel mention resolution beyond what §28 already built — this section extends where the existing composer appears and what its toolbar can insert, not what the composer itself is capable of rendering.
- A role-mention picker or composer for the pre-event ping's own fixed text (the "📅 **{name}** has been scheduled" line in `occurrences.py`) — only the Event's own `description` field (and Announcement/Template bodies) are user-authored text; the ping's surrounding message structure remains generated, matching §27.3/§32.3's existing scope boundary for what counts as composable content.

## 35. Event Cover Images

**Status:** Implemented, pending deployment. §35.1's migration script must run against production before the new application code is deployed, same order as every other schema-changing migration in this repo.

**Problem.** A Discord Scheduled Event supports a cover image (shown on the event's own detail page and in the guild's Events list), and this app had no way to set one — every event this app posts has always gone out with Discord's own default placeholder image, and there was no field anywhere in the Event modal for it.

**Design:**

- New `EventDefinition.cover_image_data` (nullable `Text`): the event's cover image, stored as the **full data URI** the browser's own `FileReader.readAsDataURL()` produces (`data:image/png;base64,...`) — deliberately not a bare base64 blob or a separately-tracked MIME type, since a data URI is exactly the string Discord's Scheduled Event `image` field itself expects, so the value the admin UI reads from a chosen file is the same value that reaches Discord with no server-side re-encoding step in between. `NULL`/empty means no cover image, matching `description`'s own empty-means-nothing convention.
- **Validation** (`services/validators.py`'s new `parse_cover_image_data`): accepts `data:image/(png|jpeg|jpg|gif);base64,<...>` only, capped at 8MB of base64 text (a sanity ceiling against an obviously-wrong upload, e.g. a video file selected by mistake — not a re-derivation of Discord's own actual size/format limits, which Discord's API enforces itself and remains the real source of truth for "too big" or "wrong format" once the request reaches it). Shared by `EventIn` (create) and `EventPatch` (update) the same way every other event field validator already is.
- **The PATCH null-vs-empty-string distinction**, needed because `cover_image_data` is genuinely nullable (unlike `discord_channel`/`description`, which are non-nullable columns defaulting to `""`): omitting the field from a PATCH request (or sending it as JSON `null`) leaves the event's current image untouched; sending it as an explicit empty string `""` clears it. This is the same "empty string means explicitly clear" shape `EventPatch` already uses for its non-nullable text fields, adapted so a *nullable* field gets an unambiguous third state (unchanged / cleared / set-to-this-value) instead of collapsing "leave alone" and "remove" into the same `None`.
- **`create_discord_event()`/`update_discord_event()`** (`services/discord_api.py`) both gain an optional `image` parameter, included in the Discord API payload only when truthy — omitted entirely (not sent as `null`) when there's no image, matching how these payloads already omit fields Discord doesn't need. `_post_to_one_tenant` (`routers/admin/occurrences.py`) passes `event.cover_image_data` straight through on every post, for every target tenant (owning, kingdom-wide fan-out, and explicit `EventTarget`s alike) — one image per event, shown identically across every guild it posts to, same as the event's name/description/location already are.
- **Admin UI**: a new **Cover Image** field in the Event modal (`events.js`'s `handleCoverImageFile()`/`removeCoverImage()`/`setCoverImagePreview()`), between the composer preview and the Notification Channel field — a file picker (`accept="image/png,image/jpeg,image/gif"`) that reads the chosen file client-side via `FileReader.readAsDataURL()` into a hidden field, a thumbnail preview of whatever's currently set (existing image on edit, or the just-picked file), and a **Remove** button that clears it. A client-side 8MB file-size check (`COVER_IMAGE_MAX_BYTES`) rejects an obviously-oversized pick before it's ever read into memory as base64, with the same ceiling the server-side validator enforces. `duplicateEvent()` carries the source event's cover image forward into the copy, same as every other field it duplicates.

### 35.1 Migration Plan

`migrate_add_event_cover_image.py` — idempotent, hand-written (§22's established convention): adds `event_definitions.cover_image_data TEXT`, nullable, no default. No existing rows are affected — every event predating this field has no cover image, matching its actual behavior (there was no way to set one before now).

### 35.2 Out of Scope

- Editing the cover image on an **already-posted** Discord Scheduled Event through the Sync tab's drift-reconciliation "push" action (`discord_sync.py`'s `update_discord_event` calls) — `image` is a supported parameter there for future use, but this app's own `GET` of a Discord Scheduled Event doesn't return enough information (Discord returns an opaque image *hash*, not the original bytes) to detect image drift the way it already detects name/description/location drift, so cover-image mismatches aren't part of the Sync tab's comparison. Changing an already-posted event's image today means cancelling and re-posting it.
- Server-side image resizing, cropping, or re-encoding — whatever the admin uploads (subject to the format/size checks above) is exactly what Discord receives; getting the aspect ratio Discord recommends (16:9) is left to the admin picking a reasonably-shaped source image, same as this app has never resized a Discord avatar/icon anywhere else either.
- Bulk import/export (§32.1) carrying cover images — `_BULK_EVENT_COLUMNS` is unchanged; a multi-megabyte base64 blob per row would make the CSV format actively worse for its one real use case (skimming/editing event lineups in a spreadsheet), so an imported event always starts with no cover image, set afterward through the modal if wanted.

## 37. Public Events Page: Announcements and Notification Lead Time

**Superseded by the unified event model.** Replaced by §66.6: messages are events with no duration and appear in the same list.

**Replacement planned:** §66 lists duration-less events as messages instead of announcements. Remains accurate for the running code until that ships.

**Status:** Implemented, pending deployment. Server-side query/serialization change plus client-side rendering — no migration, no new tables (every field used already existed).

**Problem.** Two gaps reported once the public events page (§26) had been in use for a while:

1. **Scheduled announcements — Discord-only text posts with no start/end time, created and managed under the Announcements tab — never appeared on the public `/t/{tenant_slug}/events` page**, even though they're a real, admin-scheduled thing members would want to know is coming (e.g. a "Daily Reset Warning" that recurs every day). The public page's data endpoints (`list_events`/`list_events_all` in `routers/events.py`) only ever queried `Occurrence`/`EventDefinition` — they had no knowledge of `Announcement`/`AnnouncementTarget` at all.
2. **Neither an event nor an announcement showed how far ahead of the thing itself a Discord notification actually goes out.** `EventDefinition.notify_minutes_before` (§-prior, driving `scheduler/reminders.py`'s pre-event reminder ping) and `Announcement.event_offset_minutes` (§27, "warn ahead of an event") both already answer exactly this question for their respective row types, but neither was exposed on the public page — a member had no way to tell, at a glance, "will I get pinged before this starts, and how early?"

**Design:**

- **Announcements on the public page, kept out of the ICS feed.** `routers/events.py`'s `_announcement_row_dict()` shapes a scheduled or already-posted `Announcement` into the same row structure an event row uses (`occurrence_date`, `start_datetime_utc`, `description`, `post_status`, etc.) so the client can group, sort, and render both kinds through one code path — distinguished by a new `"kind"` field (`"event"` or `"announcement"`) on every row. `scheduled_for` (a `DateTime(timezone=True)`) supplies both the grouping date and the display time, in place of an event row's `occurrence_date`/`start_datetime_utc`; `end_datetime_utc` and `duration_hours` are `null` (an announcement has neither). `routers/ics.py` is untouched — it never imports `Announcement` and was already a fully separate query path, so this is enforced by construction, not by an extra filter to remember: an announcement was never a calendar event (no duration/end time), so it was never a candidate for the ICS feed in the first place, independent of this section's public-events-page change.
  - **Scoping.** `/t/{tenant_slug}/api/events` includes an `Announcement` when it has an `AnnouncementTarget` row naming that tenant — the same targeting relationship the admin Announcements tab already uses to fan a post out to one or more Discord servers, so "is this announcement relevant to this alliance's page" already has an exact, existing answer with no new concept needed. The combined `/api/events` includes every tenant's announcement targets, one row per (announcement, target tenant) — mirroring PostLog's existing per-tenant fan-out semantics (an announcement sent to three Discord servers is three independent posts) rather than an event's single-row-per-kingdom-wide-Occurrence shape, since an Announcement has no equivalent "one Occurrence, several tenants" structure to begin with.
  - **Status/visibility filtering**, matching the existing event-row filters' spirit: `leadership_only == True` announcements are excluded (mirrors `EventDefinition.leadership_only`'s existing public-page exclusion exactly — both are the same "categorization flag, not a security boundary" per their own docstrings, but public visibility already treats it as "don't show this to the general public" either way). Only `status IN ('scheduled', 'posted')` are shown — `'draft'` isn't a real reachable state today (nothing in the admin UI creates one) and `'failed'`/`'cancelled'` have no useful "when will this happen" information left to display, unlike a cancelled *event*, which still shows (with a "Cancelled" badge) because its Occurrence still has a fixed, informative time slot. Windowed the same way events are: `scheduled_for` within `[today, today + WINDOW_DAYS]`.
  - **No Discord channel is shown on an announcement row.** Unlike `EventDefinition.discord_channel` (an admin-typed free-text display name), `AnnouncementTarget.discord_channel_id` stores only the raw numeric Discord snowflake — not something worth showing on an unauthenticated page with no bot-API access to resolve it to a human name.
- **Visual distinction (client-side).** An announcement row gets its own kind badge and visual accent distinct from an event row, and its own status vocabulary (Scheduled/Announced/Failed/Cancelled) rather than reusing an event's (pending/posted/active/completed/cancelled) — the two post-status vocabularies mean genuinely different things and were never meant to share a mapping. An announcement row also skips the duration meta item, since `duration_hours` is `null` for it. (The specific class/function names this originally shipped with — `announcementKindBadge()`, `.samaya-announcement-card`, etc. — belonged to the pre-§62 `events.html`; §62's redesign reimplemented this same distinction in `events-public.js`/`events.css`.)
- **Notification lead time.** Event rows now carry `notify_minutes_before` (straight from `EventDefinition`, already used server-side by `scheduler/reminders.py` — nothing new stored, just newly serialized here) and announcement rows carry `event_offset_minutes` (from `Announcement`, already existing per §27). Both answer the same question — "how far ahead of the thing itself does a Discord notification go out" — through a single shared renderer, `notifyBadge(ev)`: for an event, "🔔 Reminder *N* min before"; for an announcement, "🔔 Sent *N* min before event". Renders nothing when the underlying value is unset or `0`, same as every other optional meta item already on this page (e.g. `discord_channel`), so the overwhelming majority of events (which have no reminder configured) and announcements with no referenced event (`event_offset_minutes = 0`, the default — meaning "the announcement's own send time *is* the moment being announced") are unaffected.

### 37.1 Out of Scope

- A separate "announcements only" or "events only" filter/toggle on the public page — both kinds share one chronological list by design (the whole point was surfacing announcements *alongside* events, not adding a second page to check).
- Rendering an announcement's `body_markdown` through Discord-flavored markdown (bold/italic/headers/etc.) on the public page — shown as plain escaped text, exactly how an event's own `description` is already shown here (§26 never gave event descriptions markdown rendering on the public page either, only in the admin composer's own preview pane, §28).
- Resolving `AnnouncementTarget.discord_channel_id` to a human-readable channel name for display — would need an authenticated Discord API call this public, unauthenticated page has no business making; the field is simply omitted from announcement rows, as above.
- A reminder/lead-time notification system for announcements analogous to `scheduler/reminders.py`'s pre-event ping — `event_offset_minutes` already describes an existing, different mechanism (how far the announcement's *own send* is offset from an event it references); this section only exposes that existing number, it doesn't add a second Discord message type for announcements.

## 38. Admin Console Consolidation: Cross-Kingdom Default Views

**Status:** Implemented.

**Problem.** With enough alliances now live under Kingdom 138, the admin console's one-tenant-at-a-time model (pick an alliance in the header, every page shows only that alliance) made it hard for a kingdom-level coordinator to see what's happening across alliances without repeatedly switching. This section flips most admin pages from "single alliance, switch to see another" to "every alliance you have access to, by default — narrow with a per-page filter when you actually want one alliance," while leaving the public-facing pages (§26, §37) structurally the same (they gain alliance icons and a last-activity marquee — §38.9 — but keep their existing per-alliance/combined URL split).

**What made this feasible without new access-control work:** the app already had `get_current_tenants` (`routers/admin/deps.py`) — given `X-Tenant-Slug: *` it returns every tenant a superadmin can see (all of them) or every tenant a regular user holds a `UserTenant` grant for, and given a real slug it behaves exactly like `get_current_tenant`. This section is a UI/aggregation change built on that existing dependency, not a new permissions model — `UserTenant.role` (owner/coordinator/viewer) and the write-path dependencies that check it (`require_not_viewer`, `require_tenant_owner`, `require_superadmin`) are byte-for-byte unchanged.

### 38.1 Header and Per-Page Filtering

- The header's alliance switcher is removed entirely from `admin.html` — there is no more one global "current tenant" concept governing every tab, and `common.js`'s `api(method, path, body, skipTenantHeader, tenantOverride)` no longer falls back to an implicit current tenant: every write call passes an explicit `tenantOverride`.
- Each combined-capable tab (Dashboard, Events, Announcements, Schedule, Gantt — sharing Schedule's own filter, Post Log, Sync, Discord Config) has its own "Alliance: [dropdown]" filter, defaulting to **All** (`X-Tenant-Slug: *`, the `COMBINED_SLUG` constant). `common.js`'s `getTabFilter(tab)`/`setTabFilter(tab, slug)` persist each tab's choice independently under its own `localStorage` key (`samaya_filter_<tab>`), and `renderAllianceFilterSelect(selectId, tab, onChange)` renders and wires the `<select>` identically across every tab.
- Any modal that creates or edits a single-tenant-owned row (a new Event, a new Announcement) has an explicit **Owning Alliance** selector (`renderOwningTenantSelect()`) naming which tenant it belongs to. The Event modal only shows this on creation (an existing event's owning alliance is fixed, shown as plain text); the Announcement modal always shows it, since an announcement's own alliance and its delivery Targets (§13.3/§20) are two independent things.
- Row-level mutations against a combined list (editing/deleting a specific Event, toggling an Occurrence, a Sync "push fix" row) resolve the *owning* tenant of that specific row from whatever list the page already has cached (`EVENTS_CACHE`, `occurrenceData`, or the per-alliance data a combined GET already returned) rather than from the tab's current filter value, since a combined list mixes rows from several alliances at once.

### 38.2 Dashboard

`GET /api/status` and `GET /api/delivery-health` (`routers/admin/status.py`) moved from `get_current_tenant` to `get_current_tenants`, returning `{"alliances": [...]}` — one status/health row per accessible (or filtered-to) alliance. `dashboard.js` renders one card per alliance in `#st-alliances`, plus the existing Today's Events/Upcoming (48h) sections (each row tagged with its alliance name when the filter is "All") and the Delivery Health card, now one block per alliance. "Regenerate Now" fires once per alliance when the filter is "All", reporting how many succeeded.

### 38.3 Events, Schedule, Gantt, and Announcements

- **Events**: already combined-capable server-side; gained the `eventsFilter` dropdown and the Owning Alliance selector on "+ Add Event" (§38.1).
- **Schedule**: `GET /api/occurrences` is combined-capable; gained a `scheduleFilter` dropdown, and every per-occurrence write (`togglePostFlag`, `postOne`, `cancelOccurrence`, `postSelected`) resolves that occurrence's own owning alliance from the already-fetched `occurrenceData` rather than the tab filter.
- **Gantt**: shares Schedule's own filter (`getTabFilter('schedule')`) rather than keeping a second independent one, since it's a second layout over the same occurrence window.
- **Announcements**: `GET /api/announcements` (`routers/admin/announcements.py`) moved to `get_current_tenants`; gained the `announcementsFilter` dropdown and an always-shown Owning Alliance selector (`aOwningTenant`) in the announcement modal, distinct from the Targets list.

### 38.4 Post Log and Sync

- **Post Log**: already combined-capable; gained the `postlogFilter` dropdown. CSV export sends the current filter value directly (the export endpoint already accepts `*`).
- **Sync**: `GET /api/sync/discord` (`routers/admin/discord_sync.py`) moved to `get_current_tenants`, returning `{"alliances": [...]}` — one full drift report (matched/mismatched/discord_only/postlog_only, plus its own `tenant_slug`/`tenant_name`/`tenant_color`) per accessible alliance. The page renders one set of sections per alliance (tagged with its name whenever more than one alliance is shown), and every per-row fix action (Push to Discord, Mark Cancelled, Acknowledge) carries that row's own alliance slug as an explicit `tenantOverride`, since these are still single-tenant `require_not_viewer` endpoints under the hood.

### 38.5 Discord Configuration — Accordion by Server

`GET /api/discord/config-overview` (new, `routers/admin/discord_config.py`) groups by `DiscordServer.id` rather than by Tenant (`Tenant.server_id` has no unique constraint — more than one alliance can already share one server) and returns `{"servers": [...]}`, each entry carrying only the tenant names *the requesting caller* can see for that server — never a server's full backing list — so a coordinator with access to one of two tenants sharing a server never learns the other tenant's slug through this endpoint. `config.js` renders one `<details>` accordion section per server (guild name, channels, roles), replacing the old flat single-tenant channel/role tables.

### 38.6 Merged Access & Platform Page, and the Two-Tier Role Model

Access and Platform are one tab/view now ("Access & Platform"), both superadmin-only. Per the user's explicit instruction ("we have super users and view-only users... make the most prudent choices"), the UI's visibility model collapsed from three tiers (owner/coordinator/viewer) to two — `common.js`'s `isSuperadmin()` gates both halves, replacing the old owner-only gate on Access:

- **Access** (superadmin-only now, not owner-only): invite/member management, unchanged in behavior, with its own `accessAllianceSelect` alliance picker (invites/members are still inherently per-alliance — there's no combined form of "who has access").
- **Platform** (`#platformSection`, nested inside the Access view rather than its own tab): Kingdom/Discord Server/Tenant/User CRUD, unchanged in shape except for the Tenant modal (§38's icon feature, below) and Kingdom editing gaining the two branding fields (§38.7).
- **Audit Log** is also superadmin-only now (previously owner-only), and reads through whichever alliance is currently selected on the Access tab (`auditTenantSlug()` delegates to `accessTenantSlug()`) rather than keeping a second independent selector.

**This is a UI simplification only.** `UserTenant.role` still has three values server-side, and every write-path dependency (`require_not_viewer`, `require_tenant_owner`) still enforces them exactly as before — a coordinator or viewer who isn't a superadmin simply doesn't see the Access & Platform or Audit Log tabs in the UI anymore, the same way they never saw Platform before this change.

### 38.7 Editable Titles, and Tenant Icons

Two new nullable `Kingdom` columns (one Kingdom deployment today, so deliberately kingdom-wide, not per-tenant): `public_site_title` (default, when unset, `"Kingshot Event Schedule"`) and `admin_console_title` (default `"Samaya"`). Both are edited via `editKingdom()`'s prompt chain on the Platform section (§38.6) and served by a new public, unauthenticated `GET /api/kingdom-branding` endpoint (same trust level as the already-public `GET /api/alliances`) — `init.js` applies `admin_console_title` to `admin.html`'s masthead and tab title, and `events.html` applies `public_site_title` to its own masthead, both as a plain overlay on top of the existing hardcoded defaults so an unconfigured deployment behaves exactly as before.

**Tenant icons (added mid-build per explicit user instruction):** `Tenant.icon_image_data`, a nullable `Text` column reusing the exact `parse_cover_image_data` validator already built for `EventDefinition.cover_image_data` (§35) — a full `data:image/(png|jpeg|jpg|gif);base64,...` data URI, same null-vs-empty-string PATCH semantics (omitted/null leaves it unchanged, `""` clears it). Uploaded via a proper file input in a new Tenant create/edit modal (`platform.js`'s `openTenantModal()`/`saveTenantModal()`, replacing the old `prompt()`-chain editor, since a file picker needs a real form) and rendered in place of the generic color-dot/shield-emoji wherever an alliance is named: the admin Tenants table, and on the public pages — the masthead icon, the alliance switcher, and the per-row alliance badge in combined view (`events.html`'s `TENANT_ICONS`/`.samaya-tenant-icon`). `GET /api/alliances` and every admin tenant-listing endpoint include `icon_image_data`.

### 38.8 Per-Page "About This Page" Footer

Every admin tab (Dashboard, Events, Schedule, Gantt, Post Log, Sync, Discord Config, Access & Platform's two sections, Audit Log, Announcements) has a collapsed-by-default `<details class="about-page">` block at the bottom of its view (styled in `admin.css`), written in plain language for the coordinators actually using the panel: what each section/button does, and known limitations (e.g. Sync is a manual, on-demand check; Post Log only records what Samaya itself posted; Discord Config's accordion is read-only stored data, not a live re-fetch on every load).

### 38.9 Last-Activity Marquee on Public Pages

A thin strip on `events.html` (`#lastActivityMarquee`), under the masthead's info card, above the event list: **"📅 Last event posted: *{name}* · {time}"** or **"📢 Last message: *{name}* · {time}"**, sourced from `_last_activity_for_tenants()` (`routers/events.py`) — whichever is more recent between the scope's latest `posted` `PostLog` row and its latest `posted` `AnnouncementTarget` (checked at the **individual target's own `post_status`**, deliberately never the parent `Announcement.status` aggregate, since a multi-target announcement can succeed for one alliance and fail for another — the marquee must never claim a message landed somewhere it didn't; covered by a dedicated failed-target-exclusion test). `GET /t/{slug}/api/last-activity` scopes to one alliance; `GET /api/last-activity` covers every alliance and includes the winning alliance's name. Renders nothing (not an empty bar) when there's no delivery history yet.

### 38.10 Migration

`migrate_add_tenant_icon_and_branding.py` (idempotent, same hand-written convention as every prior migration — §22) adds `tenants.icon_image_data`, `kingdoms.public_site_title`, and `kingdoms.admin_console_title`. Run once against production before deploying this section's app code.

### 38.11 Out of Scope

- Per-tenant page-title overrides beyond the existing `Tenant.name` interpolation and the new per-tenant icon — the two title fields are deliberately kingdom-wide, matching this deployment's actual shape (one Kingdom).
- Any change to the `UserTenant`/`is_superadmin` access-control model itself, or to what any of the three roles can actually do server-side — §38.6's two-tier model is a UI visibility simplification only.
- Granular (more than two-tier) UI role distinctions — explicitly deferred per the user's own stated assumption that this deployment doesn't have enough distinct admins to warrant it yet.
- A live re-fetch of Discord's actual current channel/role list on every Discord Config page load — the accordion (§38.5) shows this app's own stored config, exactly as the old single-tenant page already did.

## 40. Feedback Form

**Status:** Implemented. Shipped together with §41–43, which share its underlying storage and public listing page (§43) — see §43's Addendum for what actually landed vs. this section's original design.

**Problem.** There is currently no channel for a community member browsing the public events page to tell the alliance/kingdom leadership "this is confusing," "it would help if X existed," or any other general feedback about Samaya itself — as opposed to a specific event or announcement being wrong (§42) or a new one being wanted (§41).

**Design.** A general-purpose feedback form, reachable from the public tickets page (§43) via a "+ Submit Feedback" button — not tied to any specific event/announcement row. Fields:

- **Category** (select): `Bug` / `Suggestion` / `Other` — stored as `Ticket.kind = 'feedback'` with `error_type` (reused column name, see §42) holding the category for a feedback-kind ticket.
- **Title** (short text, required, ~120 char limit) — becomes the ticket's headline on the public board.
- **Description** (textarea, required, same ~2000-char ceiling `Announcement.body_markdown` already uses, for consistency) — the actual feedback.
- **Contact** (optional free text — Discord handle or email) — so leadership can follow up; never shown on the public board, admin-only (see §43.3's admin surface).

Submission posts to `POST /api/tickets` (§43.1) with `kind='feedback'`, no `tenant_id` (general feedback isn't alliance-scoped), and no `related_occurrence_id`/`related_announcement_id`. On success, the visitor is shown their new ticket on the public board (§43) so they can immediately upvote-confirm it or watch it move through triage — the same "you get to see where it landed" pattern §42's error report gives.

### 40.1 Out of Scope

- Any reply/notification mechanism back to the submitter (e.g. emailing them when status changes) — `Ticket.submitter_contact` is stored for a human to read and manually reach out if they choose, not wired to `services/notifications.py`.
- Authenticated/attributed feedback (tying a ticket to a `User` row) — the public page has no login, and requiring one would defeat the point of a low-friction feedback channel.

## 41. Event/Announcement Request Form

**Status:** Implemented. Shares storage/listing with §40/§43.

**Problem.** A community member (or an alliance leader without admin access) currently has no structured way to propose a new recurring event or a one-off announcement — it happens ad hoc, over Discord DMs or in a general chat channel, with no record and no way for other members to signal "yes, we want this too."

**Design.** A request form, reachable from the public tickets page (§43) via a "+ Request an Event or Announcement" button. Fields:

- **Request type** (select): `Event` / `Announcement` — stored as `Ticket.kind = 'event_request'` or `'announcement_request'`.
- **Alliance** (select, populated from the existing public `GET /api/alliances`, plus a `Kingdom-wide / not sure` option) — stored as `Ticket.tenant_id` when a specific alliance is picked, `null` for kingdom-wide-or-unsure. Purely informational at this stage; it does **not** grant the requester any access or pre-fill an actual `EventDefinition`'s `scope`.
- **Proposed name** (short text, required) — what the event/announcement should be called.
- **Proposed timing** (free text, optional — deliberately not a real date/time picker bound to `EventDefinition`'s recurrence model, since a request is a suggestion, not a finished spec an admin can just save) — e.g. "every Saturday evening" or "one-time, sometime next week."
- **Details** (textarea, required) — what it is, why it matters, anything an admin needs to actually build it.
- **Contact** (optional, same as §40).

Submission posts to `POST /api/tickets` with the corresponding `kind` and the fields above folded into `description` (the timing/name/details are concatenated into one stored description with light labeling, rather than three separate DB columns, since nothing downstream needs to query them independently — an admin reads the ticket and manually creates the real `EventDefinition`/`Announcement` through the existing admin UI if they approve it).

### 41.1 Out of Scope

- Any automatic creation of an `EventDefinition` or `Announcement` from an approved request — this form produces a ticket for a human admin to read and act on manually through the existing admin console (§"Events"/"Announcements" tabs), exactly like every other admin-created row. Auto-materializing unreviewed community input into a real recurring Discord-posting event is a moderation risk this spec deliberately avoids.
- Per-alliance approval routing (e.g. only that alliance's owner/coordinator sees requests tagged to their alliance) — every ticket is visible to every admin on the shared Access & Platform-adjacent triage view (§43.3), same as Audit Log's kingdom-wide visibility model (§31.1).

## 42. Error-Flag Button on Events and Announcements

**Status:** Implemented. Shares storage/listing with §40/§41/§43.

**Problem.** Event details are admin-typed (times, channels, descriptions) and Discord posting has its own failure modes (§30's retry-failed-targets exists precisely because delivery can fail) — a member who spots something wrong ("this says Tuesday but it's actually Wednesday," "wrong channel," "this posted twice") currently has no way to flag it from the page where they noticed it.

**Design.** Each event and announcement row on the public events page (`events.html`'s per-row card, §37) gets a small "⚑ Report an issue" affordance — an icon-button in the row's meta line, next to the existing 🔔 notify badge, not a full-width button, so it doesn't compete visually with the row's actual content. Clicking it opens a modal (same X-close/Escape convention as every other modal on this page, §"all modals closeable") with:

- **Error type** (select), the values chosen to cover what's actually observable from a public, unauthenticated page: `Wrong date or time` / `Wrong channel` / `Duplicate posting` / `Didn't happen as scheduled` / `Other`. Stored in `Ticket.error_type`.
- **Description** (textarea, required) — free-text explanation, same length ceiling as §40.
- **Contact** (optional, same as §40).

The modal is pre-scoped to the row it was opened from — no event/announcement picker — and submission posts to `POST /api/tickets` with `kind='error'`, `tenant_id` set to that row's owning alliance, and `related_occurrence_id` or `related_announcement_id` set to that row's id (the two are mutually exclusive, matching the existing `kind: "event"`/`"kind": "announcement"` split §37 already introduced in the combined public feed). The ticket's public title (§43) is auto-derived as `"Issue: {event_or_announcement_name}"` rather than asked of the reporter, since the row itself already establishes what the report is about.

### 42.1 Out of Scope

- Any automatic action on the underlying `Occurrence`/`Announcement` (e.g. auto-cancelling on N reports) — an error flag only ever produces a ticket for an admin to read; every existing admin action (edit, cancel, retry) remains manual and unchanged.
- Rate-limiting or deduplicating repeated reports of the same underlying issue beyond what §43.2's per-voter upvote mechanism already provides — a second visitor hitting the same error is expected to upvote the existing ticket (the modal could eventually deep-link to "an issue like this may already be reported," but that lookup/matching UI is left for a future iteration, not this spec).

## 43. Public Tickets Page with Upvoting

**Status:** Implemented. This is the shared backend and public listing page for §40–42's three submission forms; see the Addendum below for exactly what shipped.

**Problem.** §40–42 each produce a piece of community input, but without a shared, visible destination there is no way for the community to see that their feedback landed anywhere, or to signal which of several open issues/requests matter most — every report currently either goes nowhere or has to be manually triaged over Discord with no visible record.

### 43.1 Data Model

A new `Ticket` table (`app/models/db.py`, its own section following the existing "grouped by which phase built it" convention):

| Column | Type | Notes |
|---|---|---|
| `id` | PK | |
| `kind` | `CheckConstraint`: `feedback` / `event_request` / `announcement_request` / `error` | |
| `error_type` | nullable text | error-report subtype (§42) or feedback category (§40); unused for request kinds |
| `title` | text | shown on the public board; auto-derived for `error` (§42), submitter-typed for the other three |
| `description` | text, same ~2000-char ceiling as `Announcement.body_markdown` | |
| `tenant_id` | nullable FK → `tenants.id` | null = kingdom-wide/general |
| `related_occurrence_id` | nullable FK → `occurrences.id` | `error` kind only, mutually exclusive with the next column |
| `related_announcement_id` | nullable FK → `announcements.id` | `error` kind only |
| `submitter_contact` | nullable text | admin-visible only, never rendered on the public board |
| `status` | `CheckConstraint`: `open` / `planned` / `in_progress` / `done` / `declined` | admin-managed triage state, default `open` |
| `upvote_count` | integer, default `1` | denormalized cache of `TicketVote` rows, incremented/decremented alongside vote writes rather than `COUNT()`-ed on every page load |
| `created_at` / `updated_at` | `DateTime(timezone=True)` | `ensure_utc()` conventions apply, as everywhere else in this codebase |

A second table, `TicketVote` (`ticket_id` FK, `voter_key` text, `UniqueConstraint(ticket_id, voter_key)`), backs the upvote mechanism below. No `User` FK anywhere in either table — this is an unauthenticated, public-facing feature by design, same trust level as the events page itself.

### 43.2 Anonymous Upvoting

The public page has no login, so "one vote per person" is approximated rather than guaranteed, the same tradeoff every anonymous-upvote system on the open web makes:

- On first visit to `/feedback`, the client generates a random UUID and stores it in `localStorage` (`samaya_voter_id`) — sent as an `X-Voter-Id` header on every vote request. This is a per-browser token, not an identity.
- `POST /api/tickets/{id}/vote` upserts a `TicketVote(ticket_id, voter_key=hash(X-Voter-Id + server-side pepper))` row (hashed so the raw client-supplied UUID is never stored verbatim) and increments `upvote_count`; calling it again for the same ticket/voter pair deletes the row instead and decrements (a toggle, not an accumulator) — the vote button on `/feedback` reflects this as filled/unfilled per ticket, from a `GET /api/tickets` response that includes each ticket's `voted_by_me` boolean when the request carries a recognized `X-Voter-Id`.
- Clearing browser storage or using another browser resets a visitor's vote weight to zero on their next vote — an accepted, disclosed limitation (mentioned in `/feedback`'s own `<details class="about-page">`-style footnote, matching §38.8's admin convention), not a bug to engineer around with IP tracking or fingerprinting.

### 43.3 Public Board and Admin Triage

- **Public**: a new standalone page, `static/feedback.html`, served by a new public route at `/feedback` (mirroring `events.html`/`ics.py`'s existing pattern of a public, unauthenticated, no-shared-JS page — §22's architectural precedent), listing every non-`declined` ticket sorted by `upvote_count` descending, each row showing its kind badge (reusing §37/§39's badge pattern — a fourth/fifth/sixth color for feedback/request/error kinds), title, description, status badge, relative "reported N ago" time, and the upvote button/count. `error`-kind tickets additionally link back to the event/announcement they reference (by name, not by admin-only id). `submitter_contact` is never included in this endpoint's public response shape.
- **Admin**: a new `routers/admin/tickets.py` (`GET /api/tickets` full-detail including `submitter_contact`, `PATCH /api/tickets/{id}` for `status` only) behind `require_not_viewer` (a viewer can see the same triage list everyone with tenant access can, per the existing viewer-role convention of §31.3, but cannot change `status`) and a new "Tickets" admin tab, listing every ticket kingdom-wide (no per-alliance filter — a small enough volume, and cross-alliance visibility is the point, same reasoning as Audit Log's §31.1 kingdom-wide scope) with a status dropdown per row. Changing status does not trigger any Discord post, email, or other notification (§40.1) — it only updates what the public board shows.

### 43.4 Migration

A new hand-written, idempotent migration script (`migrate_add_tickets.py`, same no-Alembic convention as every prior schema change) creates `tickets` and `ticket_votes`. (Historical: since §65, new schema changes use Alembic revisions instead.)

### 43.5 Out of Scope

- Comments/discussion threads on a ticket — upvoting is the only interaction the public board offers; richer discussion is left to the alliance's existing Discord channels.
- Merging duplicate tickets, or any admin tool beyond the status dropdown — an admin who spots duplicates handles it by setting the weaker one to `declined` with (informally, over Discord) a pointer to the surviving ticket; no in-app merge/redirect mechanism.
- Search or filtering on the public board beyond the fixed upvote-count sort — with the volume this deployment expects, a flat sorted list is legible without it; can be revisited if the ticket count grows enough to warrant it.
- Any change to `services/notifications.py` (email) or Discord posting triggered by ticket activity — every notification path in this section is "a human reads the admin Tickets tab," not an automated alert.

### Addendum — Implementation Notes

What shipped matches the design above with a few concrete choices worth recording:

- **Routes.** `routers/tickets_public.py` (new, mounted at the app root alongside `events.py`/`ics.py`): `GET /feedback` (serves `static/feedback.html`), `GET /api/tickets` (public listing), `POST /api/tickets` (create — feedback/event_request/announcement_request/error), `POST /api/tickets/{id}/vote` (anonymous toggle). `routers/admin/tickets.py` (new, wired into `routers/admin/__init__.py`): `GET /api/tickets` (full detail incl. `submitter_contact`, behind `get_current_tenant` — logged in with access to *some* tenant, header required but the query itself is kingdom-wide and ignores it), `PATCH /api/tickets/{id}` (status only, behind `require_not_viewer`).
- **`voter_key` derivation.** `hashlib.sha256(f"{SECRET_KEY}:{raw_voter_id}")` — reuses the app's existing `SECRET_KEY` (`services/sessions.py`) as the pepper rather than provisioning a second secret; this deployment already refuses to boot without it (`main.py`).
- **Public listing statuses.** `open`/`planned`/`in_progress`/`done` all show on the public board (so the community can see triage progress, not just a raw inbox); only `declined` is hidden — a small addition beyond the original "every non-declined ticket" wording, which this is consistent with.
- **Admin visibility.** Given to every logged-in user with access to at least one tenant (viewer included, read-only) via a new always-visible "Tickets" tab in `admin.html`/`js/tickets.js` — not gated behind `isSuperadmin()` the way Access/Audit are (spec §38's UI simplification), since kingdom-wide cross-alliance visibility here is the point for every coordinator, not just superadmins.
- **Public events page integration (§42).** `events.html`'s `buildEventCardHtml` gained `reportIssueButton(ev)` in the meta line (next to the 🔔 badge, `event.stopPropagation()` so it doesn't also open the Discord-preview modal) and a "Report an Issue" modal; the events header also gained a plain "💬 Feedback" link to `/feedback`.
- **Not yet built:** any deep-link from the error-flag modal to a possibly-already-reported duplicate (§42's Out of Scope already called this out), and comments/discussion on a ticket (§43.5).

## 44. Roadmap / Deferred Ideas

Small, well-scoped ideas that have come up but are deliberately not built yet — kept in one place rather than scattered across chat history, so they aren't reinvented or lost. Not commitments or a priority order; each becomes its own real section (with a real `##` number) once it's actually picked up.

- **Recent past events on the public events page.** A short "Recently Happened" list on `events.html`, covering roughly the last 48–72 hours, above or below the main upcoming list. Scope deliberately narrow: only an `Occurrence` whose `post_status` is `posted` or `completed` (i.e. it actually made it onto the calendar and, where applicable, actually went out to Discord) and an announcement whose delivery to that alliance actually reached `posted` on at least one `AnnouncementTarget` — never a `cancelled`/`failed`/merely-`scheduled` row, since the point is "what genuinely happened," not "what was supposed to happen." Needs a new `WINDOW_HOURS_PAST`-bounded query alongside the existing `WINDOW_DAYS`-forward one in `routers/events.py` (both the per-alliance and combined `GET /api/events` variants), most naturally returned as a second array (`past`) alongside the existing list rather than merged into one chronological feed, so the client can render it as its own, clearly-separated section rather than mixing "still coming" and "already done" in one list.

## 45. Sync Tab False Positives: Naturally-Completed Events and a Striped-Row Rendering Bug

**Superseded by the unified event model.** The Sync tab no longer exists (§66.7); the delivery log replaces it.

**Status:** Implemented.

**Problem 1 — every event that finishes normally was reported as a possible Discord deletion.** The Sync tab's "PostLog Only" bucket exists to catch a Discord Scheduled Event deleted directly in Discord rather than through Samaya, but it was firing for the overwhelming majority-case instead: an event that simply ran to completion. Two compounding causes, both in place since early in this app's life and only now surfacing as user-visible noise:

- `routers/webhooks.py`'s `GUILD_SCHEDULED_EVENT_UPDATE`/`GUILD_SCHEDULED_EVENT_DELETE` handlers, meant to flip `PostLog.status` to `completed`/`cancelled` the moment Discord reports either happening, can never actually run. Discord's "Interactions Endpoint URL" (what this route's signature verification and PING handling exist for) only ever delivers Interaction objects — PING, application commands, message components, modal submits. `GUILD_SCHEDULED_EVENT_UPDATE`/`_DELETE` are Gateway dispatch events, sent only over a bot's persistent Gateway (WebSocket) connection, never as an HTTP POST to any webhook URL — and this app, a stateless FastAPI service with no connected Gateway client, was never going to receive them here. The two handlers are kept in place (commented as known-dead code) rather than deleted, since the route's actual, working responsibility — PING/interaction verification — lives in the same function.
- Consequently `PostLog.status` for a finished event sits on `posted` forever. Discord's own "list scheduled events" endpoint stops surfacing a `COMPLETED` event from that listing fairly quickly — so `_sync_discord_for_tenant` (`routers/admin/discord_sync.py`) would find the PostLog row's `discord_event_id` missing from Discord's response and report it under "PostLog Only," worded as "may have been deleted directly in Discord," with a red issue count and a "Mark Cancelled" action — actively misleading for an event that succeeded.

**Fix.** `_sync_discord_for_tenant` now computes, from Samaya's own schedule data (`EventDefinition.start_time_utc` + `duration_hours` + the PostLog row's `occurrence_date`), whether the occurrence was already due to have ended by the time of the check. A PostLog row missing from Discord's list *and* already past its own end time is bucketed into a new `naturally_completed` list instead of `postlog_only`, explicitly excluded from `summary.issues` (the count behind the Sync tab's red badge), and rendered on the Sync page as a calm, green "✅ Naturally Completed" section ("Nothing to fix; mark them completed to clear them from this report") rather than the red "🔴 PostLog Only" one. A row missing from Discord but *not* yet due to have ended keeps the original "possible deletion" treatment unchanged — that combination is still genuinely suspicious. A new fix action, `POST /api/sync/mark-completed/{post_log_id}` (mirroring the existing `mark-cancelled` endpoint), lets a coordinator explicitly close a row out to `PostLog.status = "completed"`, at which point it drops out of every future sync's `WHERE status == "posted"` scan entirely. `_last_activity_for_tenants` (`routers/events.py`, spec §38.9) was widened from `PostLog.status == "posted"` to `status IN ("posted", "completed")` so marking a row completed doesn't make it disappear from the public page's "last activity" marquee.

**Problem 2 — even-numbered table rows had an unpainted gap under the Actions column.** `admin.css`'s zebra striping (`tr:nth-child(even) td { background: var(--bg2) }`) paints each `<td>` individually, but two Actions cells (Templates and Announcements, both in `announcements.js`) were built with `style="display:flex;gap:6px;flex-wrap:wrap"` set directly on the `<td>` itself. A `<td>` isn't meant to carry `display:flex` — doing so puts it in a different formatting context than its sibling cells, and when a row's other cells wrapped onto a second line (e.g. an announcement with several Targets), that flex-formatted cell no longer reliably filled the row's full height the way a normal table cell does, leaving an unpainted white strip under Actions in even rows.

**Fix.** Both cells now keep a plain `<td class="pf-v6-c-table__td">` (default `display: table-cell`, so it stretches to match the row like every other cell) with the flex layout moved onto an inner `<div style="display:flex;gap:6px;flex-wrap:wrap">` wrapping the buttons instead. No other table in the admin console uses this pattern (checked directly — `announcements.js`'s Templates and Announcements tables were the only two occurrences of `<td ... style="display:flex...">` in the codebase).

### 45.1 Out of Scope

- A real Discord Gateway connection (so `GUILD_SCHEDULED_EVENT_UPDATE`/`_DELETE` could genuinely be received) — a persistent WebSocket client is a materially different deployment shape (a long-running process rather than a stateless request-served FastAPI app) and out of scope for what fixing this particular symptom needs; the schedule-derived heuristic above is sufficient for reconciliation purposes.
- Automatically calling `POST /api/sync/mark-completed/{id}` for every naturally-completed row on a schedule — left as a one-click coordinator action for now, consistent with every other Sync fix action being manually triggered rather than automatic.

**Addendum (spec §49 batch).** The Sync page's user-facing label was shortened from "Naturally Completed" to plain "Completed" (the stat card, section heading, and the "PostLog Only" section's cross-reference sentence, all in `sync.js`) — "naturally" read as unnecessary hedging to coordinators reading the report. The internal `naturally_completed` field/list name, the `mark-completed` endpoint, and every code comment are unchanged; this is a display-text-only change.

## 46. Month Calendar View and Discord Preview

**Status:** Implemented.

**Problem.** Two related gaps on the public events page, plus one shared across the admin console: (1) the public page only ever showed a flat chronological list — there was no month-at-a-glance way to see how a week's events land relative to each other; (2) neither the public page nor the admin console gave any way to see what an event or announcement would actually look like once it reaches Discord short of posting it for real, so "does this description read right," "is the cover image going to look OK," or "did I get the placeholders right" could only be answered by publishing and then checking Discord directly.

### 46.1 List/Calendar Toggle (public events page)

**Superseded by §62** for its implementation details (this section's `setEventsView()`/`getEventsView()`, orange/purple day-cell items) — §62's redesign kept the List/Calendar toggle and the `samaya_events_view` `localStorage` key (explicitly unchanged, per §62) but rewrote the calendar's rendering in the new `events-public.js`/`events.css`, including a selected-day detail panel below the grid that this section didn't have. The still-true parts: List remains the default view, the calendar buckets events onto days using the visitor's selected display time zone (not UTC), and it's still built from the same already-fetched event data with the same 28-day-forward window — no new endpoint.

### 46.2 "Preview as it would look on Discord"

Every event/announcement card on the public page, and every row on the admin Dashboard's occurrence cards, Schedule table, Events table, and Announcements table, is now clickable and opens a "Discord Preview" modal (X-close/Escape, same convention as every other modal in this app) rendering an approximation of how that item would actually appear on Discord:

- **Announcements** render as a plain Discord message bubble (avatar, "Samaya BOT" header, timestamp, body).
- **Events** render as a Scheduled-Event-shaped card instead (title, 🗓️ start time, duration/channel meta, description, a disabled "✓ Interested" button, and the cover image banner when `cover_image_data` is set) — a plain chat bubble would misrepresent what an Event actually looks like on Discord.

Both reuse the exact placeholder-resolution and Discord-markdown-to-HTML rendering the Announcement/Template/Event-description composer preview already built (spec §27/§28/§32.2 — `clientRenderPlaceholders`/`renderDiscordMarkdownPreview`), rather than a third implementation: on the admin side directly (`common.js`'s new `openDiscordPreview(kind, item)`, called from `dashboard.js`/`schedule.js`/`events.js`/`announcements.js`'s row click handlers), and as a deliberately-simplified duplicate on the public page (`events.html`, no shared JS with admin per §22) that renders role/channel mentions as generic "@role mention"/"#channel mention" labels rather than resolved names, since a public unauthenticated page has no access to a guild's actual role/channel list the way the logged-in admin composer does.

An `EventDefinition` row in the admin Events tab (as opposed to a concrete `Occurrence`) has no single "next occurrence" to preview against, so it previews using its own `anchor_date`/`start_time_utc` — the same convention the Events modal's own live description preview (§32.2) already uses. `_occurrence_dict` (`routers/admin/serializers.py`) gained two previously-omitted fields, `description` and `cover_image_data`, so the Dashboard/Schedule preview has the same content Discord itself actually received.

Every row-level click handler (`handleRowPreviewClick`, `common.js`) ignores clicks on an actual control inside the row (a button, a checkbox, a `<select>`) so Post/Cancel/Edit/Duplicate/the post-to-Discord toggle keep working exactly as before — only a click on the row's otherwise-inert surface opens the preview.

### 46.3 Out of Scope

- Browsing calendar months outside the existing 28-day-forward data window — see §46.1; would need a different backend query shape than exists today.
- A live re-fetch of Discord's actual current role/channel names for the public page's preview — the public page has no authenticated path to that data at all (see §46.2); this is an inherent limitation of previewing from an unauthenticated context, not a bug.
- Any interactivity in the preview itself (actually clicking "Interested," reacting, replying) — it's a static visual approximation, not an embedded Discord widget.

## 48. Reserved

(Number skipped in the working session that produced §47/§49 — no content was ever assigned to it.)

## 49. Combined Owning-Alliance/Scope Selector, Event/Announcement Reassignment, and In-Place Announcement Editing

**Superseded by the unified event model.** Replaced by §66.4a edit scopes and `PATCH /api/events/{id}`.

**Replacement planned:** §66 merges announcement editing into event editing. Remains accurate for the running code until that ships.

**Status:** Implemented.

**Problem.** Three related gaps, all raised together: (1) creating an Event required picking an "Owning Alliance" from one `<select>` *and* separately ticking a "Kingdom-wide" checkbox — two controls for one underlying choice, and editing an existing event hid the alliance picker entirely (moving an event to a different alliance wasn't possible at all); (2) Announcement had no scope/kingdom-wide concept whatsoever, unlike Event, so the two forms felt inconsistent and there was no way to label an announcement as "this is for the whole Kingdom" the way an event's scope does; (3) an already-scheduled Announcement could only be Cancelled, Deleted, or Duplicated — never edited in place, so fixing a typo in a not-yet-sent announcement meant cancelling it and recreating it from scratch (losing its id/audit trail) or using Duplicate and then cancelling the original.

**Fix:**

- **Combined selector (`renderOwningTenantScopeSelect`, `common.js`)** — one `<select>` whose options are every alliance twice: once as a plain alliance option and once as that alliance's "🌐 Kingdom-wide (posted via `<name>`)" option, grouped into two `<optgroup>`s. The value encodes both pieces (`alliance:<slug>` / `kingdomwide:<slug>`); `parseOwningTenantScopeValue()` splits it back into `{scope, slug}`. This replaces the old plain `renderOwningTenantSelect` + a separate `#mKingdomWide` checkbox for both the Event modal (`events.js`) and the Announcement modal (`announcements.js`) — `renderOwningTenantSelect` itself is unchanged and still used where there's no scope concept at all (Access tab's alliance picker).
- **Events: editable, not just at creation** — the Owning Alliance row in `admin.html`'s Event modal is no longer hidden on edit; `openEventModal()` always renders the combined selector, pre-filled from the event's current owner/scope. `saveEvent()` now always sends `scope`, and on an edit additionally sends `owning_tenant_slug` in the payload — the X-Tenant-Slug header stays the event's *original* owning tenant (so `update_event` can find and authorize the row), while `owning_tenant_slug` carries where it should move *to*.
- **Backend reassignment (`EventPatch.owning_tenant_slug`, `routers/admin/events.py`)** — `update_event` resolves the new slug via `resolve_target_tenants` (the same access check Events'/Announcements' explicit multi-target lists already use), so moving an event requires access to *both* the old and new alliance, not just the old one. Picking kingdom-wide (on the new owner) still requires `check_kingdom_coordinator` for the *new* tenant's Kingdom. `discord_channel`/`notification_channel_id`/`notification_role_id` are real Discord snowflake IDs tied to one guild — reassigning across two tenants on *different* `DiscordServer`s (spec §25) clears them rather than silently carrying over IDs that belong to the wrong guild; reassigning between two tenants sharing the same server leaves them untouched, since they're still valid.
- **Events table Alliance column bug fix (`events.js`)** — the column previously showed the literal word "Alliance" for every non-kingdom-wide row (a leftover `scopeLabel` bug) instead of the owning tenant's name; it now shows the tenant's color dot + name, or "🌐 Kingdom-wide (via `<name>`)". The event-name column no longer appends a redundant `(tenant name)` tag, since the Alliance column now carries that on its own.
- **Announcements gain the same scope concept (`Announcement.scope`, migration `migrate_add_announcement_scope.py`)** — a new `scope` column (`'alliance'`/`'kingdom-wide'`, same CHECK-constraint shape as `EventDefinition.scope`), picked from the same combined selector in the Announcement modal. It's a display/ownership label only: delivery still goes entirely through the existing explicit `AnnouncementTarget` rows (the pre-existing "+ Add all alliances" button, spec §29, is still how a kingdom-wide announcement actually reaches every alliance) — picking kingdom-wide doesn't change *how* or *where* it's sent, only what the announcement is labeled as *for*.
- **Announcements: editable in place (`AnnouncementPatch`, `PATCH /api/announcements/{id}`)** — a new endpoint alongside the existing create/cancel/retry/delete ones, gated to `status == 'scheduled'` only (once posted, the message already went out; once failed/cancelled, the row is terminal — same terminal-state reasoning `delete_announcement` already applies). Supports the same `owning_tenant_slug` reassignment as events (no Discord-field-clearing needed here, since Announcement has no bare channel/role fields of its own — only its explicit targets, each already tied to its own tenant). The admin table gained an **Edit** button (shown only while `status == 'scheduled'`, next to Cancel) that reopens the compose modal pre-filled with the announcement's current title/body/schedule/targets/scope, via the same `openAnnouncementModal(source, isEdit)` the "New"/Duplicate/"Use template" flows already used — `isEdit=true` additionally prefills the existing Send Date/Time (a duplicate still leaves those blank, since a copy always needs a fresh future schedule) and routes `saveAnnouncement()` to `PATCH` instead of `POST`.

### Out of Scope

- Automatically re-populating a kingdom-wide announcement's targets when new alliances join the Kingdom, or removing them when `scope` changes back to `alliance` — targets remain a fully independent, explicitly-managed list either way (see the scope's own "display/ownership label only" note above).
- Changing an Announcement's owning alliance's Discord identity implications — since Announcement has no bare `discord_channel`/notification fields of its own (unlike Event), there's nothing analogous to clear on reassignment.

### Addendum — Standardized Table Columns and Real Notification Target Names

Follow-up feedback on the same batch: the Alliance Events, Leadership Notifications, and Announcements tables (all in the admin Events/Announcements tabs — not the Schedule tab's per-occurrence tables, which are a different concern) had inconsistent, ad hoc column sets, and every "Targets" column only ever showed raw tenant names or bare Discord snowflake IDs, never the actual channel/role/server a coordinator would recognize.

**Fix.**

- All three tables now share one column set: **Name, Alliance, Interval, Schedule Start, Status, Notification Targets, Actions** — dropping the Alliance Events/Leadership tables' previous separate Duration/Anchor columns (still visible via Edit and the row's Discord-preview click) and adding a new Alliance column to the Announcements table (previously missing one entirely).
- **Notification Targets column (`enhanceNotificationTargetLabels`, `common.js`)** — since the channel/role IDs involved are real Discord snowflakes, not names, each cell first renders with the bare ID (all that's known synchronously) inside a `data-notif-channel="tenantSlug:id"`/`data-notif-role="tenantSlug:id"` span, then a shared helper resolves every such span to a real `#name`/`@name` label — one batched `/api/discord/channels`+`/api/discord/roles` fetch per distinct tenant slug appearing in the table (cached), not one per row or per target. For events: the row's own primary channel (already a plain name, no resolution needed), its pre-event ping channel/role, and a summary line per extra `EventTarget`. For announcements: each `AnnouncementTarget`'s owning alliance name, its Discord server name (`Tenant.server_name`, already returned by `/api/tenants`), and its channel.
- **Alliance Events section background tint** — `admin.css`'s existing `--bg2` token now tints the "🛡️ Alliance Events" heading the same way `--bg3` already tinted "👑 Leadership Notifications," so the two sections read as equally-deliberate, distinct bands instead of only one of them looking styled.
- The Announcements page's stale "cannot be edited" copy was corrected to reflect §49's new in-place editing.

Out of scope: resolving channel/role names server-side into the API response itself (kept as a client-side, cached lookup — consistent with how the Announcement composer's own preview already resolves mentions).

## 50. Scheduled Announcements in the Admin Schedule View

**Superseded by the unified event model.** Replaced by the Schedule tab (§66.7), which lists every event type together.

**Replacement planned:** §66 merges announcements into the Schedule view of events. Remains accurate for the running code until that ships.

**Status:** Implemented.

**Problem.** The 28-Day Schedule tab (`#v-schedule`, both its Table and Timeline layouts) only ever showed `Occurrence` rows. A still-`scheduled` Announcement — something a coordinator is just as likely to be tracking against the same 28-day window — was invisible there; seeing it required switching to the separate Announcements tab, with no shared at-a-glance view of "everything going out in the next few weeks."

**Fix.** Both Schedule layouts gained a third, independent section for scheduled announcements, alongside (not merged into) the existing Alliance Events/Leadership Notifications sections — an Announcement has no start/end time, duration, `post_to_discord` toggle, or per-target Post/Cancel-to-Discord action the way an `Occurrence` does, so it was never going to fit those tables' rows.

- **Table layout (`schedAnnouncementsWrap`/`schedAnnouncementsTable`, `admin.html`; `renderScheduleAnnouncements`, `schedule.js`)** — a "📢 Announcements" table beneath the Leadership Notifications table, sharing §49's addendum column set (Name, Alliance, Interval, Schedule Start, Status, Notification Targets) but with an **Actions** column offering only **Edit** and **Cancel** — no post-to-Discord toggle or Post button (those are Events-only concepts), and no Delete either: this table only ever lists `status == 'scheduled'` announcements, and `delete_announcement` is terminal-state-only (§13.3/§27), so a Delete button here would always 400 — Delete/Retry Failed/Duplicate remain on the Announcements tab itself, where an announcement's full lifecycle (including terminal states) is visible. `editAnnouncement`/`cancelAnnouncement` (`announcements.js`) are reused as-is.
- **Timeline layout (`ganttAnnouncementsWrap`, `admin.html`; `renderAnnouncementGantt`, `gantt.js`)** — a third Gantt-style grid, reusing `buildGanttGrid` by mapping each Announcement into the same `{event_name, occurrence_date, owning_tenant_id, start_datetime_utc}` shape an `Occurrence` already has. Unlike an Event's recurrence — expanded into real `Occurrence` rows up front by `regenerate_occurrences` — a recurring Announcement only ever has one live `scheduled_for` at a time (the next send, advanced in place after each delivery by `scheduler/announcements.py`), so this plots exactly one marker per announcement, never a projected range of future sends the way a recurring event's Gantt row does.
- **Data loading (`loadSchedule`, `schedule.js`)** — fetches `GET /api/announcements` (scoped by the Schedule tab's own alliance filter, same as the existing occurrences fetch) into `announcements.js`'s own `ANNOUNCEMENTS` cache, rather than a Schedule-local one, so `editAnnouncement`/`cancelAnnouncement` work correctly from the Schedule tab even if the Announcements tab was never visited this session; the fetched list is then filtered to `status == 'scheduled'` for display in both new sections.

No backend changes — this is a display-only combination of two already-existing endpoints on one tab; `STATIC_ASSET_VERSION` bumped (`routers/admin/ui.py`) since only static assets changed.

### Out of Scope

- A combined single table/timeline mixing Events and Announcements row-for-row — kept as separate sections given how different their underlying shapes are (range vs. point-in-time, different action sets).
- Showing anything other than `status == 'scheduled'` announcements here — posted/failed/cancelled ones remain a concern for the Announcements tab, not a forward-looking 28-day schedule.

## 51. Daily Auto-Post Job

**Superseded by the unified event model.** Replaced by the delivery engine (§66.4); the same-name and same-time rules are kept.

**Status:** Implemented.

**Problem.** Posting an occurrence to Discord was entirely manual: a coordinator had to check the "Post?" box (or leave the default unchecked, in which case nothing happened at all) and click "Post"/"Post Selected" for every upcoming occurrence, every day, across every alliance. Nothing acted on a coordinator's behalf even for the routine case of "this occurrence is coming up and definitely needs to go out."

**Fix.** A fourth scheduler job, `scheduler/auto_post.py`'s `auto_post_upcoming_occurrences`, runs once daily at **UTC 16:00** — deliberately after regeneration's own UTC 00:00 slot, so a fresh day's occurrences are always in place first. For every `Occurrence` starting within the next **7 days** whose `post_status` isn't already `posted`/`cancelled`:

- **Nothing on Discord yet** — posted normally, through the exact same target-resolution path the manual Post button uses. `post_occurrence`'s inline kingdom-wide-fan-out-plus-explicit-targets logic was factored out into `_resolve_post_targets(db, event, owning)` (`routers/admin/occurrences.py`) so the manual endpoint and this job can't drift on what "every target for this occurrence" means; `_post_to_one_tenant` itself (already a plain, non-FastAPI function) is reused as-is, targets and all — same PostLog-reservation-before-Discord-call race protection, same per-target independent success/failure.
- **A matching Discord event already exists** (same name, start time within 15 minutes of what Samaya would have posted — `MATCH_TOLERANCE_MINUTES`) — this is a Discord Scheduled Event created directly, bypassing Samaya entirely. Rather than create a duplicate, the job records a `PostLog` row against the existing `discord_event_id` with no second Discord API call at all (`posted_by="system (auto-post, synced from Discord)"`).
- **A same-named Discord event exists but its time doesn't match** — flagged, not overwritten: `Occurrence.post_status` is set to `'error'` and `status_detail` explains the conflict, surfacing via the Schedule page's existing `⚠` status-detail indicator (no new UI needed — this reuses the same field/rendering an ordinary posting failure already uses). The coordinator resolves it deliberately through the existing Discord Sync tooling (`routers/admin/discord_sync.py`) rather than the job guessing which side is "right."
- **Skipped, not touched at all** — an occurrence whose `EventDefinition.active` is `False`; one starting in under 15 minutes (the same floor `post_occurrence` already enforces on the manual button, since something this imminent is better left to a coordinator's own judgment call); one outside the 7-day window; one already `posted`/`cancelled`.

Deliberately **not** gated on the `post_to_discord` checkbox — that flag defaults to unchecked, so requiring it would just reintroduce the daily per-occurrence chore this job exists to remove. The checkbox (and the manual Post/Post Selected buttons) remain fully available on the Schedule page for anything a coordinator wants to handle by hand.

### Out of Scope

- Diffing an existing-but-matching Discord event's description/channel/cover-image against what Samaya would have posted — matching is judged on name + start time only; a finer-grained content diff is what the Discord Sync tab's own drift report already does for posted occurrences, and is not duplicated here.
- Any change to the 15-minute-before-start cutoff, the 7-day window, or the 16:00 UTC run time being configurable per-tenant — all three are fixed constants for now (`WINDOW_DAYS`/`MIN_LEAD_TIME_SECONDS`/`MATCH_TOLERANCE_MINUTES` in `scheduler/auto_post.py`, the `CronTrigger(hour=16, minute=0, ...)` in `main.py`).

### Addendum — Corrected "Kingdom-wide" Wording

Follow-up feedback: the combined owning-alliance/scope selector's kingdom-wide options (§49) read "🌐 Kingdom-wide (posted via `<name>`)", and the Events/Announcements/Schedule table badges read "🌐 Kingdom-wide (via `<name>`)". Both wordings imply that alliance's Discord server is what actually posts a kingdom-wide item — it isn't: an Event still fans out to every alliance's own server independently (`_resolve_post_targets`), and an Announcement's `scope` is a display/ownership label only, with delivery entirely governed by its explicit `AnnouncementTarget` list regardless of scope. The named alliance is only ever used to look up which Kingdom to scope to. Reworded everywhere to "🌐 Kingdom-wide (via `<name>`'s Kingdom)", which says what's actually true without implying anything about where the posting happens.

## 52. Deduplicated Discord Posting for Alliances Sharing One Server

**Status:** Implemented.

**Problem.** Two alliances can share one `DiscordServer` (`Tenant.server_id` has no unique constraint — see spec §25). Kingdom-wide's automatic fan-out (`_resolve_post_targets`) resolves its target list by *Tenant*, not by *DiscordServer*, so posting a kingdom-wide occurrence to two tenants sharing one guild called `create_discord_event` twice — leaving two duplicate Scheduled Events sitting side by side in that one guild, and (if both tenants happened to have the same notification channel configured) two identical pre-event pings in the same channel. This affected both the manual Post button (`post_occurrence`) and the daily auto-post job (§51), since both go through `_post_to_one_tenant`.

**Fix.** `_post_to_one_tenant` now checks, before creating a new Discord Scheduled Event, whether any other tenant has already posted this same occurrence to the same `discord_guild_id` (a plain `PostLog` query — `event_id` + `occurrence_date` + `discord_guild_id` + `status='posted'`, not scoped to the current tenant). If one is found, `_record_shared_guild_post` records this tenant's own `PostLog` row referencing the *same* `discord_event_id` — no second `create_discord_event` call — while still resolving and sending this tenant's own pre-event ping if it has a notification channel/role configured (two tenants sharing a guild can still have genuinely distinct per-alliance channels worth pinging separately; only the underlying Scheduled Event itself is deduplicated, not per-tenant notifications). The pre-event ping message-building logic was factored out into `_send_pre_event_ping` so both the normal create path and this reuse path build the identical message.

Each tenant sharing the guild still gets its own `PostLog` row (so its own Dashboard/PostLog view shows the occurrence as posted, and its own Cancel action still works independently — cancelling one tenant's copy doesn't affect the shared Discord Scheduled Event itself if another tenant is still relying on it existing, though this app doesn't currently guard against that gap, see Out of Scope below).

**Announcements are unaffected by this change** — they were never at risk of this kind of duplication in the first place, since an Announcement has no automatic fan-out at all: delivery is entirely governed by its explicit `AnnouncementTarget` list (spec §13.3), so a coordinator already has full control simply by not adding two target rows that would point at the same channel. No code change was needed there.

### Out of Scope

- Guarding against a tenant's Cancel action deleting the shared Discord Scheduled Event out from under another tenant still relying on it existing — `cancel_occurrence_discord` cancels via whichever tenant's own `PostLog.discord_event_id` it's given, and since sharing tenants now share that same ID, cancelling from either tenant deletes the one real Discord object both were pointing at. A coordinator on a shared server should coordinate who cancels, same as they'd need to coordinate on any other shared-server action today.
- Deduplicating identical notification channels across tenants sharing a guild — each tenant's configured `notification_channel_id`/`notification_role_id` still gets its own independent ping; if two tenants happen to share the exact same channel, that channel still gets pinged twice. Leaving one of the two tenants' notification fields blank remains the way to avoid that, same as before this fix.

## 53. Public Notification Channel Name

**Status:** Implemented.

**Problem.** The public events page showed an event's lead-time-before-reminder (🔔 badge) but never *where* that reminder would actually post — a member had no way to know which Discord channel to watch for an event or announcement's ping without already having admin access.

**Fix.** `routers/events.py` gained `_resolve_channel_name(tenant, channel_id)`, which resolves a raw Discord channel ID to its display name via `services/discord_api.get_guild_channels`, using whichever bot token is already available for that tenant's `DiscordServer` (falling back to `PLATFORM_BOT_TOKEN`). Results are cached in-process per `DiscordServer.id` for 5 minutes (`_CHANNEL_CACHE_TTL_SECONDS`) — the public page has no bot token of its own and can get bursty unauthenticated traffic, so resolving on every single page view was never reasonable. A transient Discord API error serves the last-known-good cached mapping rather than blanking out a name that was showing fine a moment ago; no channel configured, or no cache and no live answer, resolves to `null`.

Both public endpoints now attach `notification_channel_name` to every row: `GET /t/{tenant_slug}/api/events` resolves each event via `_resolve_notification(db, event, tenant)` (imported from `routers.admin.occurrences` — the same function the admin posting path uses to decide which channel/role to ping) plus `_resolve_channel_name`, and each announcement via its own `AnnouncementTarget.discord_channel_id` for that tenant. `GET /api/events` (combined view) does the same using each row's owning tenant for events, and each target tenant for announcements (mirroring how that endpoint already fans announcements out one row per target tenant).

**Deliberately channel name only, not role name** — a channel answers "where to watch" without exposing internal role/ping-targeting structure (e.g. "@Officers") to an unauthenticated visitor.

### Out of Scope

- Showing the role that gets pinged — see above; only the channel is exposed publicly.
- A live Discord API call per page view — see the caching rationale above; a coordinator who renames a channel won't see the public page reflect it for up to 5 minutes.

## 55. Lower-Friction "Add to Calendar"

**Status:** Implemented.

**Problem.** The public events page's "Subscribe to Calendar" control was a plain link straight to the raw `.ics` feed URL. Clicking it just downloaded a file — most people, especially Google Calendar users, had no idea what to do with a downloaded `.ics` file next (Google Calendar's own "import a file" flow, buried in Settings, only does a one-time import anyway, not a live subscription the way opening the feed URL directly does).

**Fix.** Replaced both "Subscribe to Calendar" buttons (`events.html`'s header and its banner card) with an "📅 Add to Calendar ▾" dropdown (`calendarMenuHtml()`, `toggleCalendarMenu()`) offering:

- **Google Calendar** — `https://calendar.google.com/calendar/render?cid=<encoded absolute .ics URL>`, opens Google Calendar directly in the "add this calendar" flow with the feed URL already filled in, as a live subscription rather than a one-time import.
- **Apple Calendar / Outlook (desktop)** — a `webcal://` link (the same URL with its scheme swapped), which the OS hands to whatever calendar app is registered as the default subscribe handler (Calendar.app on macOS/iOS, desktop Outlook on Windows) — no download step at all.
- **Outlook.com** — `https://outlook.live.com/calendar/0/addfromweb?url=<encoded>&name=<title>`, same "opens pre-filled" pattern as the Google option.
- **Download .ics file** — the original plain link, kept as the fallback for any other app.

Both dropdowns are built from the same `calendarMenuHtml()` so the two locations never drift out of sync. `ICS_ABSOLUTE_URL` (`window.location.origin + ICS_URL`) is required here — Google's and Outlook's own URL schemes need a fully-qualified address, unlike the relative path the page used internally before this.

### Out of Scope

- Any server-side change — the underlying `/ics/events.ics` feed itself is unchanged; this is a client-side presentation fix only.
- A native in-app "Add to Calendar" prompt (e.g. detecting the visitor's platform and only showing the one relevant option) — showing all four and letting the visitor pick was judged simpler and more robust than platform-sniffing, which is unreliable and would still need a fallback anyway.

## 56. Grouped Announcement Rows in the Combined View

**Status:** Implemented.

**Problem.** `GET /api/events` (the combined, all-alliances view) intentionally returns one row per `(announcement, target tenant)` — a kingdom-wide announcement sent to every alliance is genuinely that many independent Discord posts, mirroring `PostLog`'s own per-tenant fan-out for events (see §37's addendum and `routers/events.py`'s own comments). Unlike a kingdom-wide *event*, which is a single `Occurrence` row and only ever shows once, an announcement with several targets rendered as several back-to-back cards with identical titles, times and bodies, distinguished only by a small alliance badge — in practice this read as accidental duplication (reported against `https://ks138.taraka.dev/events`, the "All Alliances" view).

**Fix.** Client-side only, in `events.html`. `groupCombinedAnnouncements()` runs once on the fetched rows (right after `loadEvents()`'s `fetch(API_URL)`, before either the list or the calendar view renders from `EVENTS_DATA`) and collapses every announcement row sharing the same `id` — an announcement's own `Announcement.id` is identical across all of its `AnnouncementTarget` fan-out rows in the combined payload — into a single card carrying a `targets: [{tenant_name, tenant_slug, tenant_color, notification_channel_name}, ...]` array. `tenantBadge()` renders one badge per entry in `targets` when present, instead of the single `tenant_name`/`tenant_slug`/`tenant_color` every other row still carries directly. The Discord preview modal (`openDiscordPreview`) adds a note when a grouped announcement has more than one target, naming every alliance it actually went to and clarifying that the preview's `{alliance_name}` substitution uses only the first one — each alliance's real post resolves its own name/channel independently at delivery time (§27), so a single client-side preview can't show all of them simultaneously.

Only the combined view is affected (`COMBINED_MODE` gate, same as `tenantBadge()`'s existing one) — a single alliance's own `/t/{slug}/events` page only ever sees its own targets, one row per announcement already, nothing to group.

### Out of Scope

- Any change to the API response shape or the per-tenant fan-out data model itself — `GET /api/events` still returns one row per target; grouping is purely a presentation step in the client.
- Per-alliance detail inside the grouped card (e.g. showing each alliance's own delivery status individually) — the card shows one shared `post_status`/badge from whichever target row happened to be first; a target-by-target breakdown would need real UI work this pass didn't scope in.

## 57. Deterministic Grouped-Announcement Badge Order

**Status:** Implemented.

**Problem.** §56's `groupCombinedAnnouncements()` collapses an announcement's fanned-out rows into one card, but the query behind it has no `ORDER BY` on the tenant side of the join — two same-timestamp announcements could come back with their `AnnouncementTarget`/`Tenant` rows in a different order each fetch, so the resulting badge order (e.g. "MOD, HTD" on one card, "HTD, MOD" on the next) looked inconsistent between otherwise-identical cards.

**Fix.** `groupCombinedAnnouncements()` sorts each grouped announcement's `targets` array alphabetically by `tenant_name` after grouping, once per card (only when there's more than one target — a single-target row has nothing to sort).

## 59. Calendar Month Grid Shows Adjacent-Month Days

**Superseded by §62** for its implementation (`renderCalendar()`, `buildCalCell()`, `.cal-cell-adjacent`, all on the pre-redesign `events.html`). The behavior itself carried forward: §62's calendar grid still fills leading/trailing weeks with the adjacent month's real days (rendered with an `is-adjacent` class) rather than leaving them blank, and clicking one still opens that day's detail in place rather than switching months (see Out of Scope below, still accurate).

### Out of Scope

- Clicking an adjacent-month day switching the grid to that month — it opens the same day-detail panel in place instead, consistent with clicking any other day in the grid. Still true under §62's rewrite.

## 60. Simplified "Add to Calendar" and Scope Clarification

**Status:** Implemented.

**Problem.** Two things about §55's "Add to Calendar" redesign: (1) the page carried a second, redundant "Subscribe to stay up to date" banner card partway down, duplicating the header's own dropdown with no added function — just a second button that did the same thing; (2) nothing on the page said *which* schedule the subscription covers — the ICS feed a visitor gets depends entirely on whether they're on one alliance's own page (`/t/{slug}/events`) or the combined all-alliances page (`/events`), and that distinction was invisible at the point where someone decides to subscribe.

**Fix.** Removed the second banner card and its `calendarMenuBtnBottom`/`calendarMenuListBottom` elements entirely — one "📅 Add to Calendar ▾" button, in the header, is enough. Added `CALENDAR_SCOPE_NOTE` (`events.html`), a short line at the top of the dropdown itself stating in plain terms what subscribing here includes: "Subscribing here adds only `{ALLIANCE}`'s events" on a single alliance's page, or "...every alliance's events, combined into one calendar" on the combined page — computed directly from `TENANT_SLUG`/`COMBINED_MODE` (already known synchronously at page load) rather than waiting on the async alliance-name fetch, so it's correct immediately.

### Out of Scope

- A way to subscribe to more than one specific alliance (e.g. two out of three) in a single feed — the feed is still exactly "this one alliance" or "every alliance," matching the two views the page itself offers.

## 61. Kingdom-Wide Visibility Decoupled From Notification Targets

**Status:** Implemented.

**Problem.** Reported directly against the public pages, with screenshots: (1) an announcement set to `scope="kingdom-wide"` never showed a "🌐 Kingdom-wide" badge — `_announcement_row_dict()` (`routers/events.py`) hardcoded `"scope": None` on every response, a leftover from before `Announcement.scope` existed at all (§49), so the column was write-only from the admin UI's own perspective. (2) The only way to get an alliance's badge to show up on the public pages at all — for either an event or an announcement — was to configure an explicit notification target (an `EventTarget` row, spec §20, or an `AnnouncementTarget` row) naming that alliance's own Discord channel. Visibility/badging and "where does the Discord ping go" were the same mechanism, so a coordinator who wanted an alliance-scope event or announcement to simply *show up* on another alliance's page — with no interest in that alliance also getting a duplicate Discord notification — had no way to do that without also wiring up a channel. And a kingdom-wide item had no dedicated automatic-fan-out equivalent for announcements at all: `EventDefinition.scope="kingdom-wide"` already drove automatic same-Kingdom visibility for events, but `Announcement.scope="kingdom-wide"` did not — every kingdom-wide announcement needed one explicit `AnnouncementTarget` row per alliance to appear anywhere outside its own owning tenant's page, and even then it showed with per-alliance badges (§56/§57's fan-out), never a single "🌐 Kingdom-wide" one.

**Fix.** Two independent changes, kept independent on purpose (bullet 3 of the request):

- **Visibility ("does this show, and with what badge") — `routers/events.py`, both `GET /t/{slug}/api/events` and `GET /api/events`.** An alliance-scope event or announcement is visible on the owning tenant's own page (unchanged) plus any tenant with an explicit target row (`EventTarget` for events, `AnnouncementTarget` for announcements) — this part is unchanged behavior, just no longer the *only* path to visibility. A `scope="kingdom-wide"` item is now visible on every tenant in the same `Kingdom` as its owner automatically, with zero target rows required — mirroring the fan-out `EventDefinition.scope="kingdom-wide"` already gave events, now extended to `Announcement.scope`. The per-tenant query joins `EventTarget`/`AnnouncementTarget` as an *outer* join scoped to the requesting tenant (so it never fans out rows — at most one match per item) and widens each `OR` clause with the kingdom-wide same-Kingdom check (announcements need `aliased(Tenant)` as `OwnerTenant` to read the *owning* tenant's `kingdom_id`, since the query has no other join to it). The combined view splits each item type into two queries instead: the owning-tenant/single-row query (unchanged), plus a second fan-out query over `EventTarget`/`AnnouncementTarget` — both filtered to `scope != "kingdom-wide"` and (for events) `tenant_id != owning_tenant_id`, so a kingdom-wide item that also happens to carry explicit targets for delivery routing is never counted twice.
- **Badging (client, `events.html`).** `_announcement_row_dict()` no longer hardcodes `scope: None` — it passes through the real column value, which is what makes `scopeBadge()` (already correct, unchanged) start rendering "🌐 Kingdom-wide" for announcements at all. `tenantBadge()` gained an early `if (ev.scope === 'kingdom-wide') return '';` — a kingdom-wide item shows only the scope badge, never an alliance badge, however many `EventTarget`/`AnnouncementTarget` rows it happens to have configured underneath for delivery. The former `groupCombinedAnnouncements()` (announcement-only, §56) is generalized to `groupCombinedFanoutRows()`, keyed by `${kind}:${id}` instead of announcement id alone, so an alliance-scope event's `EventTarget` fan-out rows on the combined view get the same one-card-with-multiple-badges treatment an announcement's `AnnouncementTarget` fan-out already had — multiple alliance badges now only ever appear for a non-kingdom-wide item with more than one valid destination, per the request's last bullet.
- **Notification targeting (`_resolve_notification`, `routers/admin/occurrences.py`) is unchanged** — it already resolved a per-target-tenant channel/role independently of anything above, returning empty strings (which the client already renders as "no channel") when nothing is configured for a given tenant. This is what made bullet 3 ("target for notifications separate from tagging/display") already true underneath; only the visibility and badging logic needed to change to stop being solely target-driven.

### Out of Scope

- A UI affordance in the admin Targets panel clarifying that a target row now only controls delivery, not visibility, for a kingdom-wide item — the screenshot that prompted this fix was about the public page's behavior, not the admin form's wording; worth a follow-up if it causes confusion.
- Retroactively backfilling anything — no migration needed, since `EventTarget`/`Announcement.scope` are pre-existing columns/tables; this is a read-path (query + rendering) change only.

## 62. Public Events Page Redesign

**Status:** Implemented. Delivered as a design handoff package (markup, CSS, JS, and a manual test checklist) and integrated as-is, with two small backend fixes discovered along the way. **Admin pages (`admin.html` and every `admin/js/*.js` file) are explicitly untouched — this is the public `/events`/`/t/{slug}/events` page only.**

**Problem.** The public schedule page had grown, section by section (§26 through §61), into a PatternFly-themed page whose dark masthead, small link-styled controls, and This-Week/Next-Week grouping were harder to scan at a glance than a purpose-built public page should be — most recently flagged as the header's alliance switcher not reading as an interactive control (§61's border/chevron tweak was a partial answer to the same complaint this redesign fully replaces).

**Fix.** `events.html` is now markup only (no inline CSS/JS); all styling moved to a new standalone `events.css` (a light theme with a gold accent, 4.5:1-contrast text/status colors, no PatternFly — PatternFly stays in the repo for `admin.html`); all behavior moved to a new standalone `events-public.js` (one IIFE, no globals, deliberately named to avoid colliding with the admin's own `js/events.js`). The data contract, routes, and every feature are unchanged: same `GET /api/events`/`/t/{slug}/api/events`/`/api/alliances`/`/api/last-activity`/`/api/kingdom-branding` calls, same `POST /api/tickets` report flow, same `samaya_display_tz`/`samaya_events_view` `localStorage` keys, same Add-to-Calendar menu and its scope note, same combined-view fan-out grouping (`groupCombinedFanoutRows()`, carrying forward §61's exact logic: kingdom-wide items never fan out and never get a per-alliance badge), same Discord preview (message and Scheduled-Event styles) with the same placeholder/markdown rendering.

What visibly changed for a visitor:
- **Alliance filter chips** replace the old dropdown-link alliance switcher entirely. On the combined page they're click-to-filter toggle chips (kingdom-wide items always show, since every alliance takes part — there's no separate "Kingdom-wide" chip to remember to also select); on a single-alliance page they're plain links to the other alliance pages and to `/events`.
- **A "Live now / Next up" hero** with a live countdown and a "Then" list of the next three upcoming events (events only, not announcements — an announcement has no duration to count down against).
- **Schedule grouped by day** (Today, Tomorrow, then weekday name and date) in the visitor's chosen time zone, replacing the previous This-Week/Next-Week grouping.
- **Collapsed rows that expand.** Each row shows local time, UTC time, name, kind, duration, a relative time, status, and alliance crests by default; channel, reminder, description, Discord preview, and Report-an-issue open only once a row is expanded.
- **Multi-day events** appear on every day they cover: later days render a dimmed "continues, until HH:MM" row (list view) or a continuation chip (calendar view), and duration reads as whole days (e.g. "2 days") once it crosses 24 hours.
- **Calendar view** gained a selected-day detail panel below the grid (click a day to see that day's rows there) instead of only the month grid.
- Noto Sans throughout, 24-hour clock, no emoji icons (crests use `icon_image_data` when set, otherwise a shield with the alliance's initials), and an accessibility pass: skip link, visible focus rings, `aria-expanded`/`aria-pressed` on every toggle, Escape closes dialogs with focus returned to the trigger, `prefers-reduced-motion` respected, 44px minimum touch targets.

Two backend fixes surfaced while integrating this, both narrowly scoped and covered by new tests:
- **`_event_row_dict()` (`routers/events.py`) now includes `cover_image_data`.** The column has existed since §35 and both the old and new Discord-preview modal already read `ev.cover_image_data` — the field was simply never included in the API response, so the cover image silently never rendered on this page (admin's own preview always had it, since it reads straight from the ORM object rather than this serialized row).
- **The two HTML-serving endpoints (`events_page`, `events_page_all`) now bust the static-asset cache.** This page previously had nothing under `/static/` to go stale — everything was inline — so it never needed the `?v=STATIC_ASSET_VERSION` treatment `admin.html` has had since §23. Now that it loads external `events.css`/`events-public.js`, the same Cloudflare edge-caching-by-extension problem applies. Rather than reach into `routers/admin/ui.py` from a public router, the implementation (`STATIC_ASSET_VERSION`/`bust_static_cache()`) moved to a new `services/static_assets.py`, shared by both; `routers/admin/ui.py` re-exports both names under their original spelling so nothing importing from there needed to change.
- Also added `eslint.config.mjs` coverage for the new standalone `events-public.js` (a rule block for top-level `app/static/*.js`, not `app/static/js/*.js`) — the pre-existing HTML-only lint rule only ever covered inline `<script>` blocks via `eslint-plugin-html`, and this page's script is no longer inline. `app/static/js/*.js` (the admin console's scripts) remain uncovered, exactly as before this change — a pre-existing gap this redesign didn't introduce and wasn't asked to fix.

### Out of Scope

- Self-hosting Noto Sans under `static/vendor/` instead of loading it from Google Fonts — the `<link>` in `events.html` is a one-line change to make later if the external font request becomes a concern.
- Translating the page's interface text — every string lives in `events-public.js`/`events.html`, not baked into markup elsewhere, specifically so this stays straightforward later, but no translation was requested or done here.
- Covering `app/static/js/*.js` (the admin console's own scripts) with ESLint — see the note above; unrelated to this page and untouched.

## 63. Public Events Page: Alliance Branding

**Status:** Partly built (2026-10-02). Built: hex validation of alliance colors, `services/contrast.py`, a Kingdom brand color (`kingdoms.color`, revision `a1f0c0de0006`, returned by `/api/kingdom-branding`), a color field in the alliance modal, and a Kingdom modal (name, slug, titles, color) that replaced the `prompt()` chain. Frontend: brand variables, theme and banner hooks (inert). Not built: themes and banners (§63.3, §64), superseded by the design in §71 so a superadmin or Kingdom coordinator can create and edit them in the admin console, images and fonts included.

**Built behavior.** A superadmin sets an alliance color in Setup, Alliances, and the Kingdom color in Setup, Kingdoms. Both must be 6-digit hex; the alliance PATCH checks the color only when it is sent, so a legacy non-hex value (the column was never validated) survives edits to other fields, and the admin form sends the color only after the person touches it. A very light color is accepted with a note (`color_note`, under 3:1 against white) because a faint stripe is a choice, not an error. The page's "All alliances" chip, view toggle, hero accent and Kingdom-wide events use the Kingdom color; a single selected alliance uses its own. `tests/frontend/ink-fixtures.json` is the one table both `pick_ink` (Python) and `brandInk` (JS) are tested against. No Kingdom crest: the public page shows no crests.

**Problem.** `Tenant.color` is an unvalidated string, the page picks readable text color with a heuristic that is not the WCAG formula (it selects an ink below 4.5:1 for roughly one in five colors), the selected alliance does not recolor the page beyond a chip border, and `events-public.js` builds ten inline `style="..."` attributes in template strings, which this repo's own HTML/CSS rule forbids.

### 63.1 Validation and contrast (backend)

- `TenantIn.color` and `TenantPatch.color` are validated against `^#[0-9A-Fa-f]{6}$` by a new `parse_hex_color()` in `services/validators.py`. On `TenantPatch` the check runs only when `color` is present in the payload, so editing any other field of a tenant with a legacy non-hex color cannot start failing. Existing rows are never rewritten by this section.
- New pure module `services/contrast.py`: `relative_luminance(hex)`, `contrast_ratio(a, b)`, and `pick_ink(hex)`, which returns whichever of `#000000` or `#FFFFFF` has the higher contrast ratio against the color. Every 6-digit hex color reaches at least 4.58:1 with one of the two, so no color is ever rejected for contrast. The module exists to choose ink, not to gate input. A pytest sweep over the RGB cube asserts the 4.58:1 floor as an invariant.
- The frontend mirrors this in the pure section of `events-public.js`, replacing `inkOn()`. Both implementations are tested against one shared fixture table (hex, expected ink), which includes `#E65100` (black, 5.54:1; white would be 3.79:1) and `#1565C0` (white, 5.75:1).
- Tenant icons get their own validator, `parse_tenant_icon_data()`, so event cover images keep their current rules. It accepts `data:image/(png|jpeg|jpg|gif|svg+xml);base64,...` under 256 KB of text. Base64 only, because `FileReader.readAsDataURL()` produces base64 for SVG too. An SVG is rendered only through an `<img>` element and is never inlined or passed to `innerHTML` as markup; an `<img>` context executes no script, which is the entire XSS mitigation, so no server-side sanitizing is done.
- Before deploy, a read-only query on the production database lists every `tenants.color` that does not match the hex pattern, so a legacy value is a known quantity.

### 63.2 Frontend behavior

- `applyBrand(color)` sets `--brand` and `--brand-ink` on `#main` (`.page`) through `element.style.setProperty`. It runs on a single-alliance page as soon as alliances load, and on the combined page whenever the filter chip changes. Selecting "All" resets to the kingdom gold and `DARK_INK`.
- Elements that adapt: the selected filter chip (fill and text), the hero accent border, and the active view-toggle segment. Elements that never take brand color: status colors (Live, Failed, Cancelled), the per-row alliance accent (each row keeps its own `--c`, since several alliances share one list), the Add to calendar button, dialogs, and the Discord preview. Kingdom-wide items keep the kingdom color.
- Inline styles are removed from `events-public.js`. Dynamic values (`--c`, `--ink-on`, `--s`) are emitted as `data-c`, `data-ink` and `data-s` attributes, and one `hydrateVars(container)` pass after each render applies them through the CSSOM. The two static `color:#949ba4` styles become CSS classes. Every color still passes `safeColor()` first.
- Cache: bump `STATIC_ASSET_VERSION` in `services/static_assets.py`.
- Tests: vitest covers `pick_ink` parity and `applyBrand` reset logic against the jsdom hook; `tests/frontend/events-public.test.js` gains the exported names.

### 63.3 Theme presets

Themes are CSS presets in `events.css`, selected by a `data-theme` attribute on `<html>` and keyed by an id from the shipped allow-list (`season_default` plus one per season or holiday). A preset overrides tokens only (`--bg`, the gold accent family, `--hero-banner`). A hero banner is a static file under `/static/themes/`, never an upload and never inside an API payload. `.hero-banner` reserves space with `aspect-ratio`, and its `background-color` plus gradient are declared beneath the image layer, so a missing or failed image shows the gradient with no layout shift. Every new preset's text and background token pairs must reach 4.5:1 before it ships. Which preset is active is decided by §64.

### Out of Scope

- Uploading banners, and any per-alliance theme.
- Alliance leaders editing their own color or crest; a superadmin does it in the Platform tenant modal.
- Analytics for the engagement targets in the product brief; the page has none today.
- Renaming existing flat CSS classes to BEM. New classes use BEM.

## 64. Scheduled Theme Resolution

**Status:** Superseded by §71 (2026-10-02), except §64.1 (windows), which §71 reuses. Kept as design history.

**Problem.** A seasonal base theme, a week-long celebration and a one-day event theme must switch on and off by date with no code change, and one date has to be that date for players in every time zone.

### 64.1 Windows

- **Global day.** For calendar date D, the window starts at 10:00:00 UTC on D-1 (UTC+14 midnight) and ends at 12:00:00 UTC on D+1 (UTC-12 end of day): 50 hours. Example: April 4 runs 10:00 UTC April 3 to 12:00 UTC April 5.
- **Build-up.** The end stays at the global-day end. The start moves back exactly 168 hours from the global-day start.
- `services/theme_windows.py` holds two pure functions, `global_day_window(date)` and `buildup_window(date, days=7)`, unit-tested against the April 4 example and a DST-adjacent date.

### 64.2 Data model

New table `scheduled_themes`: `id` Integer primary key (every other table here uses Integer keys), `kingdom_id` FK to `kingdoms` (branding is per Kingdom), `theme_id` Text, `start_utc` and `end_utc` `DateTime(timezone=True)`, `priority_level` Integer 0 to 1000. `CHECK (start_utc < end_utc)` and a named constraint on `priority_level`, so `services/db_errors.py` can translate violations. Index on `(kingdom_id, start_utc, end_utc)`. Convention for tiers, not enforced: 10 base season, 50 week-long celebration, 100 global day. `theme_id` is validated on write against the shipped preset allow-list.

There is no `Kingdom.theme_id` column. The base season is a priority-10 row.

Schema change goes through the first real Alembic revision (`alembic revision --autogenerate`, `alembic check` in CI). Production must be `alembic stamp head` on the baseline before `upgrade head`; confirm that before deploy. The change is additive, so a code-only rollback is safe, and `docs/rollback-runbook.md` covers the migration.

### 64.3 Resolution

`services/themes.py::resolve_active_theme(rows, now)` is pure and takes an injected clock:

1. Keep rows with `start_utc <= now < end_utc` for the Kingdom.
2. Sort by `priority_level` descending, then `start_utc` descending (the most recently started wins), then `id` descending, so overlapping equal-priority rows always resolve the same way.
3. Return the first row's `theme_id`, or `season_default` when none is active.

Datetimes read from the database pass through `ensure_utc()`, since aiosqlite drops `tzinfo`. `GET /api/kingdom-branding` calls it with `datetime.now(timezone.utc)` and returns `theme_id` alongside the existing titles. Unknown ids read from the table also resolve to `season_default`.

### 64.4 Client refresh and caching

The page fetches branding at load and again on the existing 10-minute interval, and sets `data-theme` on `<html>` from the result. `/api/kingdom-branding` is sent with `Cache-Control: no-cache`, and the Cloudflare cache rules for `/api/*` are checked before deploy so a rollover is not delayed at the edge. That check is open; it has not been made yet.

### 64.5 Admin

Superadmin-only `GET/POST/PATCH/DELETE /admin/api/scheduled-themes` (`require_superadmin`), each mutation through `services/audit.log_change`. The Platform-section table that edits these rows is a second patch; until then rows are created through the API. No new public route, so no Caddyfile change.

### Out of Scope

- Generating recurring yearly holiday windows automatically.
- Per-alliance schedules.
- Previewing a future theme on the public page.

## 65. Audit Remediation, Phases 1 to 5

**Status:** Implemented, repository commit `4cb4d15`. This section is reconstructed from `CLAUDE.md`, which recorded the work as it happened, because the spec was not updated at the time. No behavior visible to a visitor changed; this is correctness, safety and tooling work.

**Phase 1: schema management.** `app/alembic/` was added with a tool-generated, `alembic check`-verified baseline revision (`6fc935931248`) matching the schema as of §62. `main.py`'s lifespan stopped calling `create_all()`. Production must adopt the baseline with `alembic stamp head`, not `upgrade head`, since it already has the schema from the ten hand-written scripts; whether that was done in production is not recorded here and needs confirming. CI gained an `alembic-baseline` job (`upgrade head`, then `alembic check`) so a model change with no migration fails the build.

**Phase 2: shared services.** Discord posting logic (`_post_to_one_tenant`, `_resolve_notification`, `_resolve_post_targets`, `find_post_log`, `_send_pre_event_ping`, `_record_shared_guild_post`) moved from the admin routers into `services/discord_posting.py`, removing about seven function-local imports that existed only to avoid circular imports. `services/db_errors.py` translates `IntegrityError` into friendly 422 responses for both SQLite and Postgres, replacing substring matching on driver text, and fixing two update endpoints that previously returned a 500 on a duplicate slug or guild ID.

**Phase 3: admin and feedback markup.** `admin.html` and `feedback.html` lost their inline `<style>`, `style` attributes and inline event handlers. `feedback.html` was split into `feedback.html`, `feedback.css` and `feedback.js`. A generic `.hidden` utility replaced direct `.style.display` writes. `common.js` gained `focusModal()`, `unfocusModal()` and delegated handlers for tabs, modal dismissal and the markdown toolbar. The `/feedback` page began busting the static cache like the events page.

**Phase 4: frontend and service tests.** A `__SAMAYA_TEST__` hook in `events-public.js` exports its pure functions for vitest without running its DOM wiring. New tests cover `services/notifications.py` and `services/discord_oauth.py`, which had no direct coverage.

**Phase 5: CI, recovery and abuse limits.** Ruff runs in CI. A Docker build job boots the image against a real Postgres. `docs/rollback-runbook.md` documents three recovery procedures. The three public ticket endpoints sit behind an in-memory sliding-window rate limiter per client IP (create 5 per 10 minutes, vote 30 per 10 minutes, list 60 per minute), which is safe only because the app runs a single worker.

### Out of Scope

- Sharing the rate limiter across workers.
- Lint coverage for the admin `js/*.js` files.

## 66. Unified Event Model

**Status:** Planned, decided 2026-10-01. Nothing here is built. Until it ships, the sections it replaces (13, 20, 27, 37, 49, 50) stay accurate for the running code.

**Problem.** Events and announcements are two parallel systems: two tables, two recurrence implementations, two target tables, two posting paths and two sets of tests, merged back together only on the public page. One notification channel and role serve both the "has been scheduled" notice and the timed reminder, so a creation notice goes out whether or not anyone wanted it. Leadership-only items are hidden from the public page and feeds but are not otherwise handled. A reported recurring-announcement failure (§13.5) sits in the second recurrence implementation and is not being diagnosed, because the code is being replaced.

**Decisions.**
- An announcement is an event with no duration. A reminder is a delivery on an event, not a separate announcement.
- One combined Events list in the admin console. No existing event, announcement or ticket data needs to survive, and the admin console is rebuilt around this model.
- One label, the **event type**, replaces any separate importance, frequency or category label. Scope (kingdom-wide or alliance) stays its own field, and "daily" is just a recurrence of every 1 day.
- Wording is one default message with an optional per-alliance override. No per-channel wording.
- Publishing an event is silent. Notifications go to one named destination per alliance. A creation notice is not built now.
- Recurrence stays "every N days from an anchor date", plus "none" for one-off items. Monthly patterns are a later, additive change (§66.9).
- The audit log and the feedback board survive the rebuild. The feedback board also gains moderation, public responses and an archive (§66.10).
- Subset audiences stay: an event can target any set of alliances, not only one or all (decided 2026-10-01; the need is not proven yet, but requests are expected).
- Roles stay as they are (owner, coordinator, viewer, plus the kingdom-coordinator grant). Letting a superadmin limit which alliances a given coordinator may create events for is a later change (§66.9).
- The message composer, live preview and placeholders stay as built (§27, §28, §34).
- Editing a recurring event asks for a scope: this occurrence only, this and following, or all (§66.4a).
- No continuity is required from the running system. The old Discord Scheduled Events are deleted at cutover and recreated by the new engine (§66.8).

### 66.1 Data model

Kept unchanged: `kingdoms`, `discord_servers`, `user_tenants`, `user_kingdoms`, `invites`, `audit_log`, `scheduler_state`. `tenants` is kept and gains `notification_channel_id` and `notification_role_id` (the alliance's one named Notifications destination, both default empty strings). `users` is kept and gains a nullable `display_name`: the name shown publicly next to a coordinator's feedback responses. When empty, the Discord username is used. A user edits their own display name, and a superadmin can edit anyone's.

Replaced: `event_definitions`, `announcements`, `announcement_targets`, `announcement_templates`, `event_tenant_notifications`, `event_targets`, `occurrences`, `post_log`. `tickets` and `ticket_votes` keep their names and gain the moderation columns of §66.10 in the closure phase. The new occurrences table is named `event_occurrences` (decided in Phase 1) because `occurrences` stays in use until the old model is dropped.

| Table | Purpose and key columns |
|---|---|
| `event_types` | Reusable template. `kingdom_id`, `name` (unique per Kingdom), `color` (6-digit hex), `default_duration_hours` (nullable), `default_interval_days` (nullable, empty means one-off), `default_message`, `default_reminder_minutes` (JSON list of integers), `default_mention_role` (bool), `sort_order`. A seeded "General" type exists |
| `events` | The definition. `owning_tenant_id`, `type_id` (required), `name`, `scope` (`alliance` or `kingdom-wide`), `leadership_only`, `message` (default message, supports the six placeholders of §27), `location` (free text), `start_time_utc`, `duration_hours` (nullable and positive when set), `recurrence_kind` (`none` or `interval_days`), `interval_days`, `anchor_date` (date of the first or only occurrence), `until_date` (nullable; last date an occurrence may fall on, set when a series is split, §66.4a), `series_id` (shared by every part of a split series, so the admin list can show them as one), `mention_role`, `active`, `cover_image_data` (§35), timestamps |
| `event_alliances` | Audience. For an `alliance` event, one row per participating alliance (the owner included). For a `kingdom-wide` event every alliance in the Kingdom takes part and rows exist only to carry an override. Columns: `event_id`, `tenant_id`, `message_override` (nullable), `notification_channel_id` and `notification_role_id` (nullable overrides of the alliance default) |
| `event_reminders` | Delivery rules. `event_id`, `minutes_before` (0 or more; 0 means at the start), `message` (nullable, this reminder's own text, §77). Copied from the type's defaults when the event is created, so editing a type later does not silently change existing events |
| `event_occurrences` | One dated instance. `event_id`, `occurrence_date`, `start_datetime_utc`, `end_datetime_utc` (nullable when the event has no duration), `status` (`scheduled` or `cancelled`), per-occurrence overrides that are all nullable (`start_datetime_utc_override`, `message_override`), unique on `(event_id, occurrence_date)`. Regeneration never overwrites an occurrence that has an override or is `cancelled` |
| `deliveries` | Replaces `post_log`, announcement target status and `reminder_sent`. `occurrence_id` (references `event_occurrences`), `tenant_id`, `kind` (`discord_event` or `reminder`), `reminder_minutes` (the offset that produced the row, a snapshot rather than a foreign key; `-1` for `discord_event`), `due_at_utc`, `status` (`pending`, `sending`, `posted`, `error`, `cancelled`), `claimed_at_utc` (set when the tick claims the row, used to flag a stuck `sending` row), `discord_event_id`, `discord_message_id`, `detail`, `posted_at_utc`; unique on `(occurrence_id, tenant_id, kind, reminder_minutes)`. An index on `(status, due_at_utc)` serves the tick |
| `tickets`, `ticket_votes` | Same board as §40 to §43. An error report references `related_occurrence_id`; `related_announcement_id` is removed. Tickets gain the moderation fields and the `ticket_responses` table of §66.10 |
| `ticket_responses` | Public replies on a ticket (§66.10): `ticket_id`, `author_user_id`, `body`, `created_at`, `edited_at` |

Rules:
- A calendar entry exists exactly when `duration_hours` is set. That is what creates a Discord Scheduled Event, an ICS entry and an end time. Without a duration the item is a plain message: reminders only.
- Constraints: `recurrence_kind = 'interval_days'` requires `interval_days > 0`; `'none'` requires it null; `duration_hours` is null or positive; `minutes_before >= 0`.
- Placeholders resolve per alliance at send time, as in §27.

### 66.2 Audience and visibility

- Scope picks the audience: the whole Kingdom, or the listed alliances. Kingdom-wide visibility needs no target rows (§61).
- `leadership_only` events are absent from every public page, public API response and ICS feed, as today. This stays covered by a test on every public query.
- Until leadership-only Discord channels exist (roadmap, §66.9), sending a leadership event to a private channel is done with the per-event destination override on `event_alliances`.
- **Decided 2026-10-01:** a leadership-only event never creates a Discord Scheduled Event, because those appear in the guild's Events tab for everyone. It is an announcement for the leadership team, not a public calendar item. Its reminders and notifications still go to a channel, which an admin points at a leadership-only channel with the per-event destination override.
- Nobody is pinged individually. A role mention is used only when `mention_role` is true and the destination has a role.

### 66.3 Publishing and notifying

- **Publishing** creates the Discord Scheduled Event and makes the occurrence visible on the public page and feeds. It needs no channel and sends no message. Automatic publishing keeps the semantics of §51: seven days ahead, skipped inside 15 minutes, a same-named Discord event with a different time is flagged and never overwritten, and a same-named same-time event is recorded without a second API call.
- **Notifying** is the `reminder` deliveries, sent to the alliance's Notifications destination, or the event's override. The text is the effective message (the alliance override, else the default). If that is empty the system sends the default reminder text (§77.4).
- **Shared Discord servers** still produce one Scheduled Event per guild (§52); each alliance keeps its own delivery rows and its own reminder.
- There is no creation notice. Publishing never sends a channel message by itself.

### 66.4 Delivery engine

One per-minute tick replaces `send_pre_event_reminders`, `send_scheduled_announcements` and `auto_post_upcoming_occurrences`. Occurrence regeneration (daily 00:00 UTC, on demand and at startup catch-up, 28-day window) creates occurrences and their delivery rows, each with a `due_at_utc`: a `discord_event` delivery is due seven days before the start, a `reminder` delivery at start minus `minutes_before`.

The tick selects `pending` deliveries with `due_at_utc <= now` and handles each one independently:
1. Claim: set `status = 'sending'` and commit, before any Discord call.
2. Send.
3. Record `posted` with the Discord IDs, or `error` with the reason, and commit.

This gives at-most-once delivery and isolation. A failure in one delivery never aborts another, and a crash after the claim cannot cause a repeat. A `sending` row older than ten minutes is shown as an error in the delivery log and is never retried automatically. Retry is a manual action on an `error` row, as today (§30.1).

Editing an event regenerates its future `pending` deliveries and leaves `posted` ones alone. Deactivating or deleting an event cancels its pending deliveries and, where a Discord event exists, removes it as §32 and §51 do today.

**As built (Phase 2, `services/event_engine.py`).**
- *Flag.* `SAMAYA_UNIFIED_ENGINE` (default off). When on, `main.py` registers the per-minute tick and the daily generation job and does not register the four legacy jobs, so nothing posts twice. The closure phase deletes the legacy jobs and the flag.
- *Injected client.* The engine takes a `discord` object with the five functions of `services/discord_api.py`. Routes obtain it from `get_discord()` (`services/discord_client.py`); tests override it with a fake. The sandbox the engine was built in cannot reach discord.com, so every Phase 2 test runs against the fake.
- *Generation.* `sync_event_occurrences` is idempotent. It never touches a cancelled or start-overridden occurrence, and removes future occurrences the definition no longer produces. An occurrence that already posted something is kept as `cancelled` history instead of deleted. A reminder whose due time had already passed when it was generated is created `cancelled`, so creating or editing an event never fires notices for moments that are over. A `discord_event` delivery that would start within 15 minutes is cancelled for the same reason, and the tick re-checks both rules at send time. A late reminder found by the tick is still sent if the event has not started.
- *Audience.* An alliance event goes to its `event_alliances` rows (the owner alone if there are none). A kingdom-wide event goes to every alliance in the owner's Kingdom. A leadership event or an event with no duration gets no `discord_event` delivery.
- *Discord event.* Token is the server's `bot_token`, else `PLATFORM_BOT_TOKEN`. Alliances sharing a guild reuse one Discord event (§52). Before creating, the sender lists the guild's events (§51): same name and time is adopted without a create; same name at a different time is an `error` and is never changed; an event this app already tracks for another occurrence is ignored.
- *Reminder.* Channel and role come from the `event_alliances` override, else the alliance's notification destination. No channel is an `error`. Message precedence is the occurrence override, then the alliance override, then the event message, then `"{name} starts {event_time_relative}"`. The role is mentioned only when the event's `mention_role` is set.
- *Claim and recovery.* The claim is an atomic `UPDATE ... WHERE status = 'pending'` checked by row count, so two workers racing for one delivery send once. A `sending` row older than 10 minutes becomes `error` with a "may or may not have been delivered" note.
- *Routes (temporary `/api/v2` prefix).* `PATCH /occurrences/{id}` (cancel, restore, move, message override), `POST /events/{id}/split`, `GET /occurrences`, `GET /deliveries` (filters `status`, `event_id`, `kind`, `days`), `POST /deliveries/{id}/retry` (errors only), `GET /delivery-health` (trailing 7 days and how overdue the oldest pending delivery is). Event PATCH and DELETE push the change to posted Discord events and report any Discord failure as `discord_errors` while the database change stands.

### 66.4a Editing recurring events

Editing a recurring event asks which part of the series the change applies to. A one-off event has no choice and edits in place.

| Scope | What happens |
|---|---|
| This occurrence only | Writes an override on that occurrence: cancel it, move its start time (`start_datetime_utc_override`), or change its message (`message_override`). The series is untouched. Its pending deliveries are regenerated from the override; a `posted` Discord event is updated or deleted through §32 |
| This and following | Splits the series. The original event gets `until_date` set to the day before the chosen occurrence, its later pending deliveries are cancelled, and a new event is created from the chosen date with the new values, sharing the original's `series_id` and reminders. Past occurrences, deliveries and log rows stay under the original |
| All occurrences | Edits the event in place. Future pending deliveries are regenerated; `posted` ones and past occurrences stay as history |

Rules:
- A cancelled or overridden occurrence survives regeneration and any later "all" edit.
- "This and following" on the first occurrence is the same as "all".
- The audit log records the scope, the event IDs involved and the before and after values.

### 66.5 Recurrence

`services/recurrence.py` keeps `does_occur_on(anchor, interval_days, date)` for `interval_days` and adds `none` (only the anchor date). All dates are UTC. Weekly is 7.

### 66.6 Public surface

**As built (Phase 3).** Every public read goes through `services/public_events.public_rows`, which owns the visibility rules: inactive and leadership-only events never appear; an alliance's page shows events it owns, kingdom-wide events in its Kingdom, and events that list it in their audience; a cancelled occurrence shows as Cancelled only while the event's current schedule still includes that date, so history kept after a split or reschedule stays private; a moved occurrence shows its own time. In the combined view a kingdom-wide event is one row and an alliance event is one row per audience alliance, which the page groups back into one card. Row fields keep the shape `events-public.js` already read (`kind` is `event` when there is a calendar entry and `announcement` for a message with no duration), plus `type` (`name`, `color`), `reminder_minutes`, and `description` carrying the effective message (occurrence override, then that alliance's override, then the event message). `notification_channel_name` resolves the alliance destination, or the event's override, to a channel name as in §53. Last-activity is the latest `posted` delivery, skipping leadership-only events. Error reports from the page now always reference an occurrence.

Routes are unchanged (§5.1, §62). Each row carries the event type name and color. An event with a duration is an event row; one without is listed as a message on the public page and left out of the ICS feeds, matching what announcements did. The page colors rows by type, and the existing per-alliance crests remain.

### 66.7 Admin console

A smaller console, rebuilt on the same static-file stack (no build step). The tabs:
- **Events**: one list filtered by type, scope, alliance and status; one form for create and edit (type, name, message, schedule, recurrence, audience, reminders, calendar entry toggle, leadership flag, per-alliance overrides). The composer, live preview and emoji picker of §28 and §34 are reused for the message.
- **Schedule**: upcoming occurrences, as a table and a timeline.
- **Delivery log**: every delivery, with filters and manual retry; includes the delivery-health summary of §31.2 recomputed on `deliveries`.
- **Event types**: create and edit types.
- **Feedback**: the tickets triage view (§40 to §43), with status changes, dismiss, edit, delete, public responses and an archive (§66.10).
- **Setup**: alliances, Discord servers, channel and role pickers for each alliance's Notifications destination, access and invites, and each coordinator's display name (own name editable by the user, any name by a superadmin). Superadmin-only parts stay superadmin-only (§38.6).
- **Audit log**: unchanged behavior (§31.1); every new mutation calls `log_change`.

The open UI problems recorded on 2026-09-28 (broken "Add Target" and "New Announcement" buttons, announcement list clean-up, header modals for alliance and time zone) are subsumed by the rebuild. Whether the rebuild keeps PatternFly is decided when this tab set is designed.

### 66.8 Migration and build plan

- Additive revisions first, one destructive revision last, so CI stays green at every phase. **Phase 1 (`a1f0c0de0001`, additive, has a downgrade):** creates the new tables, the new `tenants` and `users` columns, the production-drift fixes and the "General" type per Kingdom. **Closure phase (destructive, no meaningful downgrade):** drops the replaced tables. The rollback for the closure revision is restoring the pre-change dump from `ops/backup.sh` (`docs/rollback-runbook.md`). Take that backup first. Setup data is kept (§66.1). Ticket rows are kept (`a1f0c0de0002` clears their references to old occurrences and announcements). Closure is `a1f0c0de0003`; the migration between them (`a1f0c0de0002`) is the feedback change of §66.10.
- Production must have adopted the Alembic baseline (`alembic stamp head`) before this revision. That is not confirmed and is a blocking check.
- **Production adopted the baseline (confirmed 2026-10-01):** `alembic_version` holds `6fc935931248`. `alembic check` on production still reports drift between the live schema and the models, because production was built by the old hand-written scripts: unique constraints on `user_tenants` and `user_kingdoms` carry Postgres default names (`user_tenants_user_id_tenant_id_key`, `user_kingdoms_user_id_kingdom_id_key`) where the models say `uq_user_tenant` and `uq_user_kingdom`, and `discord_servers.created_at` is nullable where the model says `NOT NULL`. The same check also lists `announcement_targets`, `event_tenant_notifications` and `announcement_templates`, which this revision drops. The Phase 1 revision (`a1f0c0de0001`) therefore renames those constraints (guarded, a no-op on a baseline-built database), and the models now say `NOT NULL` for `discord_servers.created_at` and `announcement_templates.created_at`, which matches production; the revision backfills any null and sets `NOT NULL` so a baseline-built database converges too. Correction recorded 2026-10-01: an earlier version of this note had the nullability direction reversed. `alembic check` passes on both a drifted and a fresh database.
- **Cutover and Discord events (decided 2026-10-01).** The old system does not need to keep posting before the cutover. The cutover step deletes the Discord Scheduled Events the old system created, identified by the `discord_event_id` values in `post_log`, and never touches events created by hand. The new engine then creates fresh ones. There is no adoption of matching events at cutover. The §51 same-name, same-time check stays for events created by hand later.
- **Test environment.** The branch runs against a separate dev database and a separate bot token (`PLATFORM_BOT_TOKEN` in the dev `.env`), with that bot invited to a private test Discord server. The dev environment never receives production credentials, because the new engine would post to real alliances.
- Because the schema is replaced, the build happens on a branch and cuts over once, not incrementally on production. Phases:
  1. **Foundation (done 2026-10-01, branch `unified-event-model`):** models, the additive revision, recurrence `none` and `until_date`, event type and event APIs at `/admin/api/v2/event-types` and `/admin/api/v2/events`, validators, 83 new tests. The `/v2` prefix is temporary and is dropped in the closure phase, when the old routes go. Edits in Phase 1 apply to the whole event; the scope choice of §66.4a arrives with the delivery engine.
  2. **Delivery engine (done 2026-10-01, branch `unified-event-model`; verified against real Discord 2026-10-01 with `ops/dev_smoke_engine.py`: one Scheduled Event and one reminder posted and cleaned up on a test server):** occurrence and delivery generation, the single tick, shared-guild dedupe, reminders, per-occurrence edits, the split, manual retry, delivery log and health, all behind `SAMAYA_UNIFIED_ENGINE`; 58 tests against a fake Discord client including at-most-once and failure isolation. Not yet exercised against real Discord: that needs the dev bot and test server.
  3. **Public surface (done 2026-10-01):** public API, ICS, last-activity and the events page read the new tables through one query path, `services/public_events.public_rows`; `tests/test_public_surface.py` checks a leadership-only event against every public route (JSON, ICS, last-activity, per alliance and combined) and was mutation-checked by removing the filter.
  4. **Admin console (done 2026-10-01):** the tabs in §66.7, built only on the surviving endpoints, checked in a real browser (Playwright against a seeded local server, desktop and 390px wide). The composer moved to `js/composer.js`. Known API gaps are in CS.11.
  5. **Closure (done 2026-10-01):** feedback moderation, responses and archive (§66.10); the old routers, scheduler jobs, models, schemas and tests deleted; the `/v2` prefix and the `SAMAYA_UNIFIED_ENGINE` flag removed (the engine is the only engine); revision `a1f0c0de0003` drops the replaced tables and refuses to run while any old Discord event ID remains in `post_log`; `app/cutover_delete_old_discord_events.py` deletes those events first (dry run by default, shared-guild events deleted once, safe to repeat); `docs/cutover-runbook.md` and the cutover section of `docs/rollback-runbook.md`; `CLAUDE.md` and Part I rewritten. Checked on a fresh Postgres 16 and on one seeded with old-model rows (guard refuses, then passes once IDs are cleared; `alembic check` clean). The cutover itself is run by the owner, not by a merge.

### 66.9 Roadmap, not built now

- Monthly recurrence kinds, each one predicate plus one enum value and one validation rule: `monthly_day` (the Nth day; for day 29 to 31 the default will be to use the last day of a shorter month) and `monthly_nth_weekday` (first to fourth or last Monday to Sunday).
- Leadership-only Discord channels (a tick box on a channel, so leadership items only go there) and a private leadership calendar.
- Per-coordinator alliance limits: a superadmin chooses which alliances a given coordinator may create events for. To be designed after the core model is built.
- A creation-notice destination and an "on publish" trigger.
- A separate importance label, if filtering across types ever becomes necessary.
- Downstream systems (player points, KVK buff slot requests, conduct and NAP information). The event ID is the attachment point; nothing is designed for them now.

### 66.10 Feedback board: moderation, responses and archive

**Requirement recorded 2026-10-01.** Today a ticket can only change status (`PATCH /api/tickets/{id}`, §43.3), the public list hides only `declined` ones, and there is no way to delete, dismiss or edit a ticket or to reply, so tickets stay on the board forever and the team cannot answer publicly. The rebuilt board must allow:

- **Dismiss:** hide a ticket from the public board without deleting it, through a new `dismissed` status (for spam or off-topic items). It stays visible in the admin Feedback tab and can be restored.
- **Delete:** remove a ticket permanently, with its votes and responses. Superadmin only, and always written to the audit log.
- **Edit:** coordinators can correct the title or text of a ticket (for example to remove personal details or fix a typo) and change its type. The original submitter is not told; the audit log keeps the before and after.
- **Public responses:** coordinators and superadmins can post replies on a ticket. Responses show on the public board under the ticket, with the author's display name and the time, so players can follow progress as the team works on it. The display name comes from `users.display_name` (§66.1) and is resolved when the page is shown, so changing a name updates earlier responses too. A response can be edited or deleted by its author or a superadmin. Responses are plain text, at most 2,000 characters.
- **Status as progress:** a ticket's current status is shown beside its responses on the public board.
- **Archive:** the public board has two sections. The main section lists active tickets (`open`, `planned`, `in_progress`). A separate, collapsed **Archived** section lists finished ones (`done` and `declined`), read-only, with their responses, so people can look up older tickets and the reasoning behind decisions. There is no time window: a ticket moves to the archive when its status changes, not after a delay. `dismissed` tickets appear in neither section.

Rules:
- Statuses become `open`, `planned`, `in_progress`, `done`, `declined`, `dismissed`. This changes today's behavior, where `declined` is hidden from the public board. A declined ticket is now public in the archive so a response can explain the decision.
- Viewers see everything and change nothing (§31.3). Coordinators and owners can respond, dismiss and edit; permanent deletion is superadmin only.
- Submitters stay anonymous. `submitter_contact` is still never shown publicly.
- The three public ticket endpoints keep their rate limits (§65); the response and moderation endpoints are admin routes behind a session.

**As built (Phase 4 backend).** Migration `a1f0c0de0002` adds the `dismissed` status and `ticket_responses`, drops `tickets.related_announcement_id` and repoints `tickets.related_occurrence_id` at `event_occurrences` (existing references are cleared; foreign keys are dropped by what they reference because production's names came from hand-written scripts). The public `GET /api/tickets` returns `{active, archived}`; `dismissed` is in neither, voting on an archived ticket is 409 and on a dismissed one 404, and each ticket carries its responses. Admin: `GET /admin/api/tickets` (all statuses, responses included), `PATCH` (status, title, description, kind, error_type; audited with before and after), `DELETE` (superadmin only, audited with the response count), and `POST/PATCH/DELETE /admin/api/tickets/{id}/responses` (author or superadmin may change or delete; viewers cannot respond). A response author is shown as the user's `display_name`, or "Team" when none is set; the Discord username is never shown publicly. `PATCH /admin/api/me` sets your own display name; a superadmin sets anyone's through `PATCH /admin/api/users/{id}`. `PUT /admin/api/notification-destination` sets the selected alliance's Notifications channel and role (alliance owner or superadmin, digits only, empty clears).

### 66.11 Disposition of earlier sections

| Section | Disposition |
|---|---|
| 13 Announcements, 27 Templates, 49 and 50 (announcement editing and schedule view), 37 (announcements on the public page) | Replaced |
| 20 Event targets | Replaced by `event_alliances` |
| 28, 34 Composer | Reused for the event message |
| 30.1 Retry | Becomes delivery retry |
| 31.1 Audit log, 31.3 Viewer role | Kept |
| 31.2 Delivery health | Rewritten on `deliveries` |
| 32.1 CSV import and export | Not carried over unless requested |
| 35 Cover images | Kept, on events |
| 40 to 43 Feedback board | Kept; error reports reference occurrences only; moderation, public responses and an archive added (§66.10) |
| 51 Auto-post, 52 Shared guilds, 53 Channel name, 61 Kingdom-wide visibility | Semantics kept |
| 62 Public events page | Kept, adapted |
| 63, 64 | Deferred |

### Out of Scope

Everything in §66.9, plus any change to authentication, Kingdoms, alliances or Discord server setup.

## 67. Destinations, Audience Groups and Deduplication

**Status:** Built and deployed 2026-10-01 (revision `a1f0c0de0004`). Destination ownership is changed by §68: where this section says a destination belongs to an alliance, §68 wins. It extends §66 on purpose: §66 listed "Discord server setup" as out of scope and gave each alliance exactly one Notifications destination.

**Problem.** An alliance has one Discord server and one notification channel and role. Two alliances that share a channel (production: MOD and NSR on one channel) each get their own reminder, so the channel sees two messages for one event. There is no way to post to a second server or channel, to build a leadership-only audience, or to point one event at a one-off channel without a per-event override.

**Decisions (2026-10-01, with the owner).**
- An alliance owns a list of **destinations**: a Discord server, a channel, an optional role and a label. A destination has a `post_by_default` flag. An event posts to every default destination of every audience alliance, unless the event opts one out or adds a non-default one.
- Each alliance has one **primary server** (the existing `tenants.server_id`). It is labelled **primary** in Setup. Scheduled Events are created and managed on the primary server only. An alliance may also list **secondary servers** (zero or more): servers it is allowed to post notifications to, shown to admins so they know where messages should and should not go. A secondary creates no Scheduled Event. Sharing is declared per alliance and never inferred: MOD and VAL may each list HTD's server as secondary, which says nothing about MOD and VAL sharing a server with each other. A superadmin assigns both. A destination's server must be the alliance's primary or one of its secondaries.
- **Servers belong to a Kingdom.** Production runs one Kingdom (138) today; the link keeps a second Kingdom from sharing or reaching another's servers.
- **Audience groups** live at Kingdom level. A group is a named, reusable set of destinations. A user with a kingdom-coordinator grant manages groups. A group expands into its destinations whenever occurrences are generated, so editing a group changes every future occurrence that uses it and no posted history.
- **Deduplication:** reminders dedupe on (Discord server, channel, offset). Scheduled Events dedupe on server. The delivery log keeps one row per destination; the duplicates are marked `cancelled` with "merged into delivery #N". Role mentions are the union of the merged roles. Different offsets (60 and 10 minutes) stay separate reminders.
- **Leadership.** A `leadership_only` event posts only to `leadership_only` destinations. A `leadership_only` destination receives only leadership events. Leadership events never create Scheduled Events and never appear publicly (§66.2, unchanged). The admin console shows a Leadership badge.
- The per-event channel and role overrides (`event_alliances.notification_channel_id`, `notification_role_id`) are removed. A one-off channel is a destination with `post_by_default` off, ticked on the events that need it. The per-alliance message override (`event_alliances.message_override`) stays.

### 67.1 Data model

New tables:

| Table | Columns and rules |
|---|---|
| `destinations` | `id`, `tenant_id` (owning alliance), `server_id` (references `discord_servers`), `channel_id` (required), `role_id` (default empty), `label` (required, unique per alliance), `post_by_default` (bool), `leadership_only` (bool), `created_at`. Unique on `(tenant_id, server_id, channel_id, role_id)` |
| `audience_groups` | `id`, `kingdom_id`, `name` (unique per Kingdom), `description`, `created_at` |
| `audience_group_destinations` | `group_id`, `destination_id`; unique pair; cascade on group delete, restrict on destination delete |
| `event_destinations` | A per-event change to one destination's default: `event_id`, `destination_id`, `included` (true adds a non-default destination, false opts out of a default). Unique on `(event_id, destination_id)` |
| `event_groups` | `event_id`, `group_id`; unique pair |

Changed tables:
- `deliveries` gains `destination_id` (nullable; null for `discord_event` rows; `SET NULL` on delete), `guild_id` and `channel_id` (snapshots, empty for `discord_event`, so history survives edits to a destination), and `merged_into_id` (nullable self reference). The unique constraint `(occurrence_id, tenant_id, kind, reminder_minutes)` is replaced by two partial unique indexes: `(occurrence_id, destination_id, reminder_minutes)` where `kind = 'reminder'`, and `(occurrence_id, tenant_id)` where `kind = 'discord_event'`.
- `discord_servers` gains `kingdom_id` (required). The revision backfills it from the Kingdom of the alliances that use the server, else from the only Kingdom when there is one, and stops with an error naming the server when it cannot decide. Creating or editing a server needs a Kingdom, and an alliance's primary and secondary servers must belong to its own Kingdom (422 otherwise).
- New table `tenant_secondary_servers`: `tenant_id`, `server_id`, unique pair; the server must differ from the tenant's primary and share its Kingdom. Secondary servers are informational plus the allowed-list for destinations; the engine reads them for nothing else.
- `tenants.server_id` is unchanged and is now documented as the **primary server**. `tenants.notification_channel_id` and `notification_role_id` stay in the schema until the closing revision (§67.8) and stop being read by the engine.
- `event_alliances.notification_channel_id` and `notification_role_id` stay in the schema until the closing revision and stop being read or written.

### 67.2 Resolving where an occurrence goes

The set of destinations for an event, computed by one function (`resolve_destinations(event)`) that the engine, the save validation and the admin preview all call:

1. Start with the destinations of every audience alliance where `post_by_default` is true (§66 audience rules: the listed alliances, or every alliance in the Kingdom for a kingdom-wide event).
2. Add every destination from the event's groups.
3. Add every `event_destinations` row with `included = true`. It must belong to an audience alliance or to one of the event's groups.
4. Remove every `event_destinations` row with `included = false`.
5. Apply the leadership rule: a `leadership_only` event keeps only `leadership_only` destinations; a non-leadership event drops them.

Save rejects (422, naming the destination) an explicit add that a non-leadership event cannot use, and an add that is neither in the audience nor in a group. The engine skips such a destination with a `cancelled` row and the reason, in case a flag changed after save.

An event that resolves to no destination creates one `error` reminder row per audience alliance reading "No destination configured" (or "No leadership-only destination configured"), as §66.4 treats a missing channel today.

### 67.3 Deduplication and merging

Within one occurrence, destinations that share (guild, channel) and a reminder offset form one **send**. The engine creates a delivery row for every destination and links them:

- **Representative:** the existing non-cancelled delivery of the group if one exists, else the first by (alliance id, destination id). It carries the real send. The others get `status = cancelled`, `merged_into_id = <representative id>` and `detail = "Merged into delivery #N"`.
- **Content:** one message. The text is the base effective message. The mention is `<@&role> ...` for the union of the merged destinations' roles (each once), and only when the event's `mention_role` is set.
- **`{alliance_name}`** renders as the alliance names of the merged destinations' owners, sorted by name and joined with ", ". `{kingdom_name}` and the time placeholders are unchanged.
- **Re-sync:** pending rows are regrouped on every sync. A `posted` or `sending` row is never rewritten. A destination that joins a send already posted is created `cancelled` with "Merged into delivery #N (already sent)".
- **Scheduled Events** keep §52: alliances whose primary servers share a guild share one Discord event (unchanged; no `discord_event` schema change). The follower rows stay `posted` with "Shared with another alliance in the same Discord server" and now also set `merged_into_id`, so the log marks them the same way. The description renders `{alliance_name}` with every audience alliance on that guild. Group and explicit destinations never create Scheduled Events. A leadership-only event creates none on any server (§66.2).

### 67.4 Conflicting messages

A per-alliance `message_override` creates a **conflict** when two or more destinations in one send resolve to different effective texts (an override against the base text counts). The occurrence override applies to all alliances and never conflicts.

- **At save:** the event create and update routes reject a conflict with 422, naming the channel and the alliances involved, and the admin form shows it inline. Nothing is saved.
- **After save** (a Setup or group change creates a conflict later): the engine sends the base event message once, does not apply the conflicting overrides, and writes the explanation on the representative row ("Conflicting alliance messages on a shared channel; base message sent. Overrides for MOD, NSR were not used"). The override rows are not deleted. The Events list shows a warning on the event, computed from the same function, until the owner resolves it.
- Nothing is sent twice and nothing is dropped silently.

### 67.5 Permissions

| Action | Who |
|---|---|
| Create, edit, delete a destination | Owner of that alliance, or superadmin |
| Assign an alliance's primary or secondary servers | Superadmin |
| Choose a destination's server | The alliance's primary or one of its secondary servers |
| Create, edit, delete an audience group | Kingdom coordinator for that Kingdom, or superadmin |
| Use a group or destination on an event | Anyone who may create or edit that event (viewers cannot) |

Deleting a destination used by an event or a group returns 409 with the list of events and groups that use it. Deleting a group used by an event returns 409 the same way. The delivery history of a deleted destination stays through the `guild_id` and `channel_id` snapshots.

### 67.6 Routes and admin console

Routes (all under `/admin`, tenant by `X-Tenant-Slug`; `*` is read-only):
- `GET/POST /api/destinations`, `PATCH/DELETE /api/destinations/{id}`.
- `GET/POST /api/audience-groups`, `PATCH/DELETE /api/audience-groups/{id}`.
- `POST /api/events/preview-destinations`: takes the event form's audience, groups, destination changes and leadership flag; returns the resolved destinations, the merged sends and any conflicts. The form calls this instead of repeating the rules in JavaScript.
- `GET /api/discord/channels` and `/roles` gain an optional `server_id` query parameter (checked against §67.5), because a destination's server is no longer the alliance's own.
- Event create, update and read gain `group_ids`, `destination_changes` (`[{destination_id, included}]`) and a read-only `warnings` list; the per-alliance channel and role fields are removed.
- `PUT /api/notification-destination` is removed; the destination list replaces it. `GET /api/deliveries` returns one entry per send, with `members` (each destination's row, status and merge link); the health counts exclude merged rows.
- `GET /api/discord/channels` and `/roles` keep the existing fallback to a text field when Discord is unreachable.

Console:
- **Setup, alliances:** a "Discord server(s)" column. Each alliance shows exactly one line "Primary: <name>" (guild ID beneath) and, when it has any, one "Secondary: <name>" line per secondary server, with no limit on how many. Editing an alliance sets the single primary and a multi-select of secondaries (a superadmin edits both; owners see them read-only) and a Destinations table: label, server, channel, role, Post by default, Leadership only, with add, edit and delete.
- **Setup, audience groups** (visible to kingdom coordinators and superadmins): name, description and a destination picker grouped by alliance.
- **Events form:** the audience stays; a Destinations panel lists the resolved destinations grouped by alliance (default ones ticked; unticking opts out; other destinations of audience alliances can be ticked in), a Groups multi-select, and a one-line summary "Posts to N destinations in M channels". Conflicts show inline.
- **Events and Schedule:** one row per event or occurrence. Audience chips, a "N destinations" badge, a Leadership badge and a warning marker. No row per alliance or per channel.
- **Delivery log:** one line per send with a "merged" marker, expandable to the per-destination rows.
- **Public page:** unchanged and still hides leadership events. `groupCombinedFanoutRows` already folds the combined view's per-alliance rows into one card with several alliance chips; the build verifies it with a test. `notification_channel_name` becomes the channel name of the alliance's first default, non-leadership destination, and a leadership-only destination is never exposed.

### 67.7 Rate limits

The cutover script showed that Discord returns 429 under bursts, and that one delete helper has no retry.
- `cancel_discord_event` gains the same bounded retry as `create_discord_event` and `send_channel_message`: honour `retry_after` from the JSON body, fall back to the `Retry-After` header, then to exponential backoff. A non-JSON 429 body no longer raises.
- Each wait is capped at 15 seconds and each call at 3 attempts, so one rate-limited send cannot stall the tick. After the last attempt the row becomes `error` with a clear reason and is retried by hand.
- `cutover_delete_old_discord_events.py` paces its deletes (0.5 seconds apart) and retries through the shared helper, so one run completes.

### 67.8 Migration and rollout

- **Revision `a1f0c0de0004` (additive, with a downgrade):** creates the five tables, adds the delivery columns and indexes, and seeds data so behavior is unchanged the moment it runs:
  - one destination per tenant with a non-empty notification channel: label "Notifications", the tenant's primary server, the tenant's channel and role, `post_by_default` true;
  - for every `event_alliances` row with a channel or role override: a destination for the override (label "Event channel", `post_by_default` false, `leadership_only` true when the event is leadership-only, reused when the same alliance, server, channel and role exist), an `event_destinations` row that adds it, and one that opts out of the alliance default. A role-only override becomes a destination on the default channel with the other role; because both destinations then share a channel, they merge and the mention is the union of both roles, which differs from the old "override replaces" rule. This is rare and is flagged here for review;
  - existing `deliveries` get `guild_id` and `channel_id` backfilled from the alliance's destination and keep their status.
  - Leadership events that relied on the alliance default channel now resolve to no leadership-only destination (§67.2) and show a clear error until the owner flags a destination. This is deliberate: it closes the path where a leadership reminder could reach a public channel.
- **Revision `a1f0c0de0006` (destructive, a later release):** drops the tenant and `event_alliances` notification columns after the new engine has run cleanly on production. Taking the `ops/backup.sh` dump first is the rollback.
- **Deploy:** a branch, a PR the owner merges, then on `lxc-taraka`: back up, `git pull`, `docker compose build app`, `alembic upgrade head`, `docker compose up -d app`, then check Setup shows the seeded destinations. Code-only rollback is safe for `0004` because the old columns remain.
- Production entry of events can continue while this is built: the seed converts what exists, and nothing is dropped until `0006`.

### 67.9 Tests

Against `FakeDiscord`, following `tests/unified_helpers.py`:
- **Shared channel (the production case):** MOD and NSR on one channel and server, one event for both, one 10-minute reminder. The fake records one message; the second delivery is `cancelled` with "Merged into delivery #N"; the mention holds both roles once; `{alliance_name}` renders "MOD, NSR". A 60-minute and a 10-minute reminder stay two sends.
- Two alliances on different servers get two messages and two Scheduled Events; a shared server gets one. A secondary server never gets a Scheduled Event.
- Server Kingdom link: backfill, 422 on a cross-Kingdom primary or secondary server, and on a destination whose server is neither the alliance's primary nor a secondary.
- Default, opt-out, explicit add and group expansion each change the resolved set as §67.2 says; editing a group changes future pending rows only.
- Leadership: a leadership event reaches only a leadership-only destination; a non-leadership event never reaches one; no Scheduled Event; a leadership event with no such destination records the error. The public-surface test for a leadership event stays and still passes.
- Conflicts: save rejects with the channel and alliances named; a conflict created after save sends the base message once with the explanation and shows a warning; nothing is sent twice.
- Permissions per §67.5, with `make_user_and_client`; delete returns 409 with the users listed.
- Migration: seed a pre-`0004` database (default channels, overrides, a leadership event), upgrade, and assert the converted destinations and event rows; `alembic check` is clean.
- Rate limits: `cancel_discord_event` and `send_channel_message` retry on 429 with JSON and non-JSON bodies, and stop after the cap.
- Frontend: `groupCombinedFanoutRows` folds two alliance rows into one card; admin pages checked in a real browser at desktop and 390px width.

### 67.10 Docs

On completion: Part I and `CLAUDE.md` describe destinations, groups and merging; the cutover runbook's Setup step becomes "configure destinations"; `docs/rollback-runbook.md` gains the `0004` and `0005` notes.

### 67.11 Not built here

Per-destination message wording, a creation-notice destination (§66.9), per-coordinator alliance limits (§66.9),and Scheduled Events on any server other than an alliance's primary.


## 68. Audiences (Kingdom-Level, Multi-Channel)

**Status:** Designed 2026-10-01 after §67 shipped, revised the same day after owner feedback. It supersedes §67.1, §67.2 and §67.5 wherever they say a destination belongs to one alliance or holds one channel, and it replaces audience groups. Merging, conflicts, leadership and rate limits (§67) stand.

**Problem.** §67 made every destination one channel owned by one alliance. Two alliances posting to the same channel (production: MOD and NSR "Leadership") each needed a copy, and one announcement that must reach several servers needed a group on top.

**Vocabulary.** "Destination" now has its ordinary meaning: **a server, a channel and an optional role to mention.** What §67 called a destination is an **Audience**: a named, reusable list of destinations.

**Decisions (2026-10-01, with the owner).**
- An **Audience belongs to the Kingdom**: label, `leadership_only`, and one or more destinations on any server of that Kingdom. A Kingdom coordinator (or superadmin) creates, edits and deletes it once.
- An **alliance uses an Audience through a link** (`alliance_audiences`: alliance, audience, `post_by_default`). The link means "this alliance may post to this Audience"; the flag means "its events post there without being asked". One Audience links to many alliances. An alliance owner (or a coordinator or superadmin) manages that alliance's links.
- **Events select multiple Audiences.** The Events form lists the Audiences linked to the audience alliances, defaults pre-ticked, and each can be switched on or off per event (`event_audiences`, as §67's per-event change).
- **Audience groups are folded into Audiences.** A multi-destination Audience does what a group did. Migration converts each group into an Audience holding the channels of its members.
- Servers need only share the Audience's Kingdom. **Secondary servers become informational**; Scheduled Events still go only to the alliance's primary server.
- **Resolution (§67.2):** step 1 takes the Audiences linked to each audience alliance with `post_by_default`. Step 3 accepts an explicit add only when an audience alliance is linked to that Audience (default or not). An Audience linked to none of them is unavailable to the event. Sharing is declared, never inferred.
- The result is a list of **targets**: an (alliance, destination) pair per destination of each selected Audience. An Audience reached by an explicit add that no audience alliance is linked to is attributed to the event's owning alliance.
- **Merging (§67.3) now spans Audiences and alliances.** Deliveries are one per (occurrence, destination, alliance, offset). Deliveries that share (guild, channel, offset) are one send, whichever Audiences or alliances produced them. `{alliance_name}` lists the alliances of the merged targets. A role shared by merged targets is mentioned once.
- Leadership (option A) is unchanged: a leadership-only Audience takes only leadership events and a leadership event uses only those.

### 68.1 Data model

- `audiences` (was `destinations`): `kingdom_id`, `label`, `leadership_only`. Unique on `(kingdom_id, label)`. No server, channel or role.
- `audience_destinations` (new): `audience_id`, `server_id`, `channel_id`, `role_id`. Unique on `(audience_id, server_id, channel_id, role_id)`. The server must share the Audience's Kingdom (422 otherwise). At least one per Audience.
- `alliance_audiences`: `tenant_id`, `audience_id`, `post_by_default`; primary key on the pair; same Kingdom; cascade on alliance delete, restrict on Audience delete.
- `event_audiences` (was `event_destinations`): `event_id`, `audience_id`, `included`.
- `deliveries.destination_id` references `audience_destinations.id`. Reminder unique index: `(occurrence_id, destination_id, tenant_id, reminder_minutes)` where `kind = 'reminder'`.
- Dropped: `audience_groups`, `audience_group_destinations`, `event_groups`.

### 68.2 Permissions

| Action | Who |
|---|---|
| Create, edit, delete an Audience and its destinations | Kingdom coordinator for that Kingdom, or superadmin |
| Link or unlink an alliance, set its default flag | Owner of that alliance, a coordinator of its Kingdom, or superadmin |
| Select Audiences on an event | Anyone who may create or edit that event |

Deleting an Audience that is linked or used by an event returns 409 listing the alliances and events. Known trade-off: an alliance owner can link any Kingdom Audience, including another alliance's leadership one. The leadership rule still limits it to leadership events. Restricting links to coordinators is a one-line permission change.

### 68.3 Routes and console

- `GET/POST /api/audiences`, `PATCH/DELETE /api/audiences/{id}` act on the Kingdom of the selected alliance. Each Audience carries `destinations` (`server_id`, `channel_id`, `role_id`) and `links` (`tenant_id`, `post_by_default`). A PATCH that sends `destinations` replaces the list.
- `PUT /api/alliance-audiences` sets the selected alliance's links: a list of `{audience_id, post_by_default}`. The console no longer calls it (see Setup below); it stays for API use and keeps the alliance-owner permission.
- `/api/destinations` and `/api/audience-groups` are removed. `POST /api/events/preview-destinations` keeps its name and now returns Audiences with their destinations.
- Setup: a **Kingdom audiences** panel (coordinators and superadmins) where each Audience has a list of server and channel rows and a checklist of the alliances that use it, each with "Post by default". There is no separate per-alliance panel, so an alliance owner who is not a Kingdom coordinator cannot change links from the console (revision of the first §68 build, which had one). The Events form shows **Audiences** as multi-select chips.

### 68.4 Migration

Revision `a1f0c0de0005` converts what §67 created:
- Each §67 destination becomes an Audience plus one `audience_destinations` row. `kingdom_id` comes from the owning alliance. Destinations with the same Kingdom, server, channel and role merge into the lowest id; a label that clashes in the Kingdom gets the alliance name appended. Each former owner becomes a link with its old `post_by_default`.
- Each audience group becomes an Audience (label from the group, a clash gets " (group)") holding the distinct channels of its members. Alliances are not linked to it. `event_groups` become `event_audiences` with `included = true`.
- `event_destinations` and `deliveries` are remapped to the surviving rows, dropping duplicates (an add and an opt-out for merged rows keeps the opt-out).
- Downgrade splits shared Audiences into per-alliance single-channel destinations and re-creates groups from groups' Audiences. Groups created after upgrade are not reconstructed (stated in the rollback runbook).
- The revision that drops the legacy `notification_*` columns becomes `a1f0c0de0006`.

### 68.5 Tests

One Audience with two channels on two servers: two sends. An Audience linked to MOD and NSR: one message, role once, `{alliance_name}` "MOD, NSR". Two Audiences sharing a channel: one message. Link default versus optional. Explicit add rejected when no audience alliance is linked. Coordinator versus owner permissions and 409 on delete. A migration test from a §67-shaped database with identical destinations and a group. Setup and Events form checked in a browser at desktop and 390px.

## 69. Refinements after the first production deploy

Small changes made once the unified model was live (2026-10-02). Part I already reflects them.

### 69.1 Anchor alliance

`events.owning_tenant_id` stays NOT NULL, so every event has an alliance. For a Kingdom-wide event the Events form calls it the **Anchor alliance**: it supplies the default message wording and the server for the Discord Scheduled Event, and nothing else. Any Kingdom coordinator can edit a Kingdom-wide event; the anchor's owner alone cannot. A nullable owner (a truly Kingdom-owned event) was considered and deferred: it needs a `kingdom_id` on events and a rule for which server hosts the Discord Scheduled Event.

### 69.2 Kingdom badge on public pages

A Kingdom-wide row from `public_rows` no longer carries `tenant_name`, `tenant_slug` or `tenant_color` in the public JSON, so players never see the anchor. The page draws a neutral "Kingdom" badge (`evAlliances` in `events-public.js`). In an alliance's own view the row still carries that alliance. ICS UIDs are built from the alliance, event name and date and are unchanged, so existing subscribers see no duplicates.

### 69.3 Delivery log sections

`GET /api/deliveries` takes `section`. `upcoming` returns `pending` and `sending`, soonest first, with no look-back bound (an overdue pending row stays in this section). `past` returns every other status, newest first, within `days`. Without `section` it behaves as before. The tab shows two tables, and a status filter loads only the table that status belongs to.

### 69.4 Setup: no per-alliance audience panel

The Setup tab no longer has an "Alliances and audiences" panel. Which alliances use an Audience, and "Post by default", are edited in the Audience editor, and the Audiences table shows "Used by". `PUT /api/alliance-audiences` stays for API use with its owner permission. Consequence: an alliance owner who is not a Kingdom coordinator cannot change links from the console.

### 69.5 Discord limits and 400 detail

`services/discord_api.py` cuts a Scheduled Event description to 1000 characters and a location to 100, ending with an ellipsis when cut, because Discord answers a longer value with 400 "Invalid Form Body". A 400 now returns the rejected fields (for example `description: Must be 1000 or fewer in length.`) in the delivery log's Detail. Channel messages are unaffected (2000 characters).

### 69.6 Considered and not built

- **Native Discord recurrence** (`recurrence_rule`): fits only every 1, 7 or 14 days, cannot set an end date, and the docs do not show how to cancel or move a single occurrence, which the per-occurrence edits need. A spike on a dev bot would settle it.
- **A 14-day Discord event lead** instead of 7: not needed, because the public pages and ICS feeds read occurrences over a 28-day window whether or not the Discord event exists.
- **Live character counter** against 1000 in the composer, for events that create a Discord event.
- **Automatic alliance tag** on the Discord event name (`M0D | Rally Night`), added by the engine so the stored name stays clean. Until then the manual convention is `TAG | Activity`, using the alliance's short name, `K138 | ...` for Kingdom-wide events and `M0D + NSR | ...` for joint ones, and a stable name once posted (the engine matches Discord events by name and time, and ICS UIDs include the name).

## 70. Discord slash commands

**Problem.** Players ask "when is the next event?" in Discord and have to open the public page. A feedback note means leaving Discord too.

**Design.** The existing `POST /webhooks/discord` endpoint (HTTP interactions, signature verified against `PLATFORM_PUBLIC_KEY`) now also handles application commands, autocomplete and modal submits. No gateway connection, no new process, no new table. Handlers live in `services/discord_commands.py`; `routers/webhooks.py` verifies the signature and dispatches. Commands are global and registered by `app/register_discord_commands.py` (dry run by default, `--apply` to send).

**Commands.**
- `/schedule [alliance] [days]`: upcoming events, 7 days by default, 1 to 14. `/next [alliance]`: the next one, with its location.
- `/feedback category`: opens a modal (summary, details) and files a `feedback` ticket on the public board, with `error_type` set to the chosen category (Bug, Suggestion, Other).

**Scope.** Everything `/schedule` and `/next` return comes from `services/public_events.public_rows`, so leadership-only and inactive events never appear. Only events with a duration are listed (not plain messages), cancelled and finished occurrences are skipped, and a Kingdom-wide event is labelled "Kingdom". Without the `alliance` option the answer covers the alliances declared on the server where the command ran (an alliance's primary or secondary server; sharing is never inferred, as in §67). A server with no declared alliance, or a direct message, gets every alliance's events. With `alliance` set, that alliance's own view is used.

**Output.** Ephemeral (only the asker sees it). Times use Discord's `<t:UNIX:f>` and `<t:UNIX:R>`, so each reader sees their own time zone. `allowed_mentions` is empty, so an event name can never ping anyone. A reply is cut to fit Discord's 2000 characters.

**Feedback details.** The ticket's alliance is set only when the server maps to exactly one alliance. `submitter_contact` stores the Discord display name and ID so moderators can follow up. The public board never returns it, and the confirmation says moderators can see it. The existing per-IP limiter cannot work here (every request comes from Discord), so a per-user limit of 3 tickets per 10 minutes applies.

**Not built.** Coordinator commands (create, cancel), personal reminders, buttons on posts. They need the Discord ID to Samaya user mapping and a permission pass, and come after read-only has proven stable.

**Setup (owner).** Slash commands belong to the application the bot token belongs to, which may differ from the OAuth login application. Use that application's Public Key for `PLATFORM_PUBLIC_KEY`, set its Interactions Endpoint URL to `https://ks138.taraka.dev/webhooks/discord`, invite the bot with the `applications.commands` scope, then run the registration script.

## 71. Public Page Theming, Page Text and Fonts

**Status:** Designed 2026-10-02. Built so far: §71.4 images for event covers and alliance icons (`services/images.py`, `reprocess_images.py`, Pillow); the banner profile exists. Built in the theme step (backend): revision 0008, fonts catalogue, rules, resolution, generated stylesheet, theme and schedule API, head injection on `/events`. Deviations: no in-process cache of the resolved theme (one small query per page view); `--gold-deep` and `--navy-hover` derive in CSS with `color-mix`, not on the server; numerals accept monospace families only (no proportional family has had its tabular figures verified); `/feedback` is themed too since its redesign uses the same tokens (the redesign came from the Kingshot Themes and Feedback Redesign handoff; its art-band theme model, §64-style `[data-theme]` blocks and `scripts/check_themes.py` were not adopted because §71 replaces them). Appearance admin tab (Themes and Schedule panels, live contrast, iframe preview via `?preview_theme=`) is built; its Page text and Kingdom branding panels are not. Header art, banner images and the extended tokens are built (§71.15). Still to build: page text overrides and `theme.copy`, banner upload, serving and hero rendering. Supersedes §63.3 (theme presets in CSS) and §64.2 to §64.5 (scheduled theme storage and admin). §64.1 (global-day windows) and §63.1, §63.2 (colors, brand variables; built) stand.

**Problem.** The Kingdom wants to change how the public pages look for a season or a festival, and what their headers and titles say, without a deploy. A superadmin should create and edit themes in the admin console, including a banner image and fonts, and schedule them by date. Today the palettes live in `events.css`, the titles are one Kingdom field, and the fonts are fixed.

**Principles.**
- A theme is data, never code. The admin edits a fixed set of values; there is no free-form CSS. A bad value can then neither break the layout nor make text unreadable.
- Every text and background pair a theme can change is checked for WCAG contrast on save, in the browser for feedback and on the server for enforcement. A theme that fails cannot be saved.
- The server resolves the active theme and writes it into the page before first paint. No script applies a theme, so there is no flash of the wrong theme.
- Page text and theme are separate systems. A theme may override some text (for example a festival title); the Kingdom's own text is the fallback.

### 71.1 What a theme contains

| Field | Rule |
|---|---|
| `name` | Required, at most 60 characters, unique per Kingdom. |
| `bg` | Page background, hex. Contrast against the fixed `ink` token at least 7:1 and against `muted` at least 4.5:1. |
| `accent` | Accent fill (the "All alliances" chip, live accents), hex. Text on it is `pick_ink(accent)` (§63.1), so no pairing rule. |
| `accent_text` | Accent-colored text (the kicker, links), hex. At least 4.5:1 against white and against `bg`. |
| `primary` | Primary buttons and the active view toggle, hex. White text on it at least 4.5:1. |
| `font_heading`, `font_body`, `font_numerals` | Keys from the font catalogue (§71.2). Null means the page default. |
| `banner_asset_id` | Optional image (§71.4). |
| `banner_overlay` | A number from 0.90 to 1.00, default 0.96. The strength of the near-white overlay between the banner and the hero text. At 0.90 or more, any image, including pure black, keeps the fixed `ink` and `muted` text above their contrast floors; a test computes the worst case. |
| `copy` | Optional JSON object of text overrides (§71.3). |
| `archived` | Hides the theme from pickers; a scheduled or base theme cannot be archived. |

Derived by the server, not edited: `--gold-deep` (accent, darker), `--navy-hover` (primary, lighter), and the live, line, tint and surface tokens stay as shipped. The generated stylesheet maps these onto the existing CSS variables (`--bg`, `--gold`, `--gold-deep`, `--gold-ink`, `--navy`, `--navy-hover`, `--font`, `--mono`) and adds `--font-heading`, `--banner` and `--hero-overlay`. `events.css` keeps the shipped defaults, so a page with no theme looks as it does today, and the `[data-theme]` presets are removed. The five seasonal palettes become starter templates in code (`services/theme_templates.py`, converted to hex and checked by the same contrast rules); the editor offers "Start from a template". No seed rows.

### 71.2 Fonts

**Catalogue in code, not free text.** `services/fonts.py` holds an allow-list of Google Fonts families. Each entry has a key, display name, the Google family string, the weights loaded (at most three), a CSS fallback stack, the scripts covered, the roles it may fill (`heading`, `body`, `numerals`) and a `tabular` flag. Adding a family is a reviewed code change. The launch list is about 15 families (the page's current Noto Sans and IBM Plex Mono, a few more neutral sans families, a few serif and display faces for headings such as Cinzel and Merriweather, and a few monospace faces); each is checked for availability, license (open source), weights and scripts when the catalogue is built.
- `numerals` accepts only monospace families and proportional families whose tabular figures were verified, so the time column does not shift.
- Decorative faces are limited to `heading`.
- A catalogue key that no longer exists (removed in a later deploy) renders as the default for that slot and logs a warning. It never fails the page.

**Loading.** One Google Fonts CSS2 request, built by the server from the resolved theme's three keys, with `display=swap` and `preconnect`. Google serves per-script chunks, so a visitor downloads only the scripts a page uses. Alliance names and translated text (§72) in scripts the font lacks fall back through the stack to the catalogue's fallback for that script, then to system fonts (§72.6). Each visitor's IP address reaches Google; self-hosting the catalogue is a later per-font change and needs no data change, because themes store keys. To limit layout shift when a heading font swaps in, each catalogue entry carries a fallback stack chosen to match its metrics.

### 71.3 Page text

A fixed registry in `services/page_copy.py` lists every editable string: key, default, page, maximum length and the placeholders it accepts (`{alliance_name}`, `{kingdom_name}`). Text only: it is length-capped, stripped of control characters, and rendered with `textContent`, never as HTML.

| Key | Where | Default |
|---|---|---|
| `events.title` | Page heading, tab title | "Event Schedule" (tab: "Kingshot Event Schedule"). Stored in the existing `kingdoms.public_site_title`. |
| `events.kicker_all` | Line above the heading, combined view | "Kingshot · All alliances" |
| `events.kicker_alliance` | Same, one alliance | "Kingshot · {alliance_name}" |
| `events.description` | Meta and link-preview description | "Upcoming events for {kingdom_name}." |
| `events.hero_live`, `events.hero_next`, `events.hero_then` | Hero labels | "Live now", "Next up", "Then" |
| `events.all_chip` | Filter chip | "All alliances" |
| `events.footer` | Footer line | "Times shown in your time zone and UTC · Powered by Samaya" |
| `feedback.title` | Feedback page heading and tab title | "Feedback & Requests" |
| `feedback.intro` | Feedback page introduction | The current paragraph |
| `feedback.back_link` | Link back to events | "← Back to Events" |

**Resolution and languages.** Every registry key is also a key in the interface catalogue of §72, and Kingdom and theme values are stored per language: `kingdoms.page_copy` is `{key: {locale: text}}`, and a theme's `copy` has the same shape. For the visitor's language the order is: the active theme's value, the Kingdom's value, then the shipped catalogue text for that language (which falls back through language, the Kingdom default and `en`, §72.4). A custom value applies only to the language it was written for; a visitor in another language sees the shipped translation, not the custom text in a language they did not choose. The admin shows each language's inherited text as the placeholder and flags keys customised in one language but not another. A blank value means "inherit". The legacy `kingdoms.public_site_title` counts as the default language's value of `events.title` until the first save of that key in the new editor.

**Delivery.** The server writes the resolved values into the HTML: the `<title>`, `<meta name="description">` and Open Graph tags at a marker in `<head>`, and a `<script type="application/json" id="page-copy">` block (with `<` escaped) that the page scripts read at start-up. Link previews in Discord, which as far as is known do not run JavaScript, therefore show the custom title and description. Both pages keep their default text in the markup, so a page still reads correctly if the block is missing.

### 71.4 Images (Pillow) and banners

Pillow is added now (decided 2026-10-02), not only for banners. One module, `services/images.py`, verifies and normalises every image the app stores, with a profile per use. The same processing then protects the Discord Scheduled Event covers and alliance icons, which today are stored exactly as uploaded (§33, §38.7) with only a regex check.

**Shared rules.** Input is a base64 data URI in JSON, as today (no multipart dependency). Encoded size at most 8 MB (the existing ceiling). PNG, JPEG, WebP or GIF; SVG is refused, because opened directly in a browser it can run script on this origin. The decoder runs with a pixel limit (`Image.MAX_IMAGE_PIXELS` at 40 million), and a decompression bomb returns a 422 naming the image. EXIF orientation is applied, then all metadata is dropped (GPS location, camera data). An animated GIF becomes its first frame. Alpha is kept or flattened per profile. The work runs in the thread pool (`run_in_threadpool`), because the app has one worker and an image decode must not stall the delivery tick. Each failure is a 422 that names the field and the reason.

| Profile | Used for | Output |
|---|---|---|
| `event_cover` | Discord Scheduled Event cover (`events.cover_image_data`) | Fit within 1600 by 800, never upscaled, flattened onto white, JPEG quality 85. The editor shows a guide for Discord's visible band: Discord's own guidance is 800 by 400 and community reports say only the central 800 by 320 shows, so keep text and faces there (widely reported, not official). |
| `tenant_icon` | Alliance icon (`tenants.icon_image_data`) | Center-cropped to a square, at most 256 by 256, PNG with alpha. |
| `theme_banner` | Theme hero banner (below) | Fit within 1600 wide, WebP quality 82. |

**Behavior kept.** PATCH semantics are unchanged: omitted or null leaves the image alone, an empty string clears it. Processing runs only when a new image arrives, and the existing `parse_cover_image_data` stays as the cheap first check in the request models; the routers then call `services/images.py` and store its output. Re-processing an already processed image is stable. The Discord engine is untouched: it still sends the stored data URI, which is now smaller and in a format Discord's cover endpoint already accepts (JPEG), so fewer oversized-image rejections. Existing stored images are not rewritten automatically; `app/reprocess_images.py` (dry run by default, `--apply`, run in the container) re-encodes stored covers and icons and reports the bytes saved. This also shrinks the admin event list, which today returns each cover inline (CS.11).

**Dependency.** `Pillow>=11` in `requirements.txt` (the first release with Python 3.13 wheels; the Docker base is `python:3.13-slim`, so the image needs no compiler). The wheels bundle libwebp. CI builds the image, which proves the install.

**Banner storage.** Table `theme_assets`: `id`, `kingdom_id`, `sha256` (of the stored bytes), `content_type`, `width`, `height`, `byte_size`, `data` (bytea), `created_at`, unique on `(kingdom_id, sha256)`. In Postgres, so the nightly `pg_dump` covers it and no volume is added. `POST /admin/api/theme-assets` runs the `theme_banner` profile.

**Banner serving.** `GET /theme-assets/{sha256}.webp`, public, `Cache-Control: public, max-age=31536000, immutable`, `X-Content-Type-Options: nosniff`. The hash makes the URL unguessable and cache-safe, and a replaced image gets a new URL. Cloudflare has no custom cache rules on this site (checked 2026-10-02), so it caches by its default list of file extensions; a `.webp` URL is on that list and `Cache-Control` is respected, which is what an immutable hashed URL wants.

**Lifecycle.** Replacing or removing a theme's banner deletes the old asset in the same transaction when no other theme uses it. Deleting a theme does the same.

**Rendering.** The hero keeps its reserved height, and its gradient and `background-color` sit beneath the image layer, so a missing or slow image shows the gradient with no layout shift (§63.3 rule, kept).

### 71.5 Scheduling and resolution

- **Base theme.** `kingdoms.default_theme_id` (nullable FK). Null means the shipped defaults. This replaces §64.2's "base season is a priority-10 row".
- **Scheduled themes.** Table `scheduled_themes` as §64.2: `kingdom_id`, `theme_id` (FK, restrict on delete), `start_utc`, `end_utc`, `priority_level` 0 to 1000, `CHECK (start_utc < end_utc)`, index on `(kingdom_id, start_utc, end_utc)`. Convention: 50 week-long celebration, 100 global day. The admin offers the §64.1 windows (global day, build-up) as one-click date ranges.
- **Resolution.** `services/themes.py::resolve_active_theme(kingdom, rows, now)` is pure with an injected clock: rows with `start_utc <= now < end_utc`, ordered by priority, then latest start, then id, all descending; the first wins; none falls back to the base theme, then to the defaults. Datetimes pass through `ensure_utc()`.
- **Deleting** a theme that is the base theme or is scheduled returns a 409 naming where it is used.

### 71.6 Delivery to the pages

- `/events`, `/events/{slug}` and `/feedback` resolve the theme and copy per request (one small query, with a short in-process cache of the result; the app has one worker) and replace marker comments in the static HTML. This removes the hard-coded Google Fonts `<link>` from both pages.
- Generated stylesheet: `GET /theme/{theme_id}.css?v={updated_at}.{STATIC_ASSET_VERSION}` (`/theme/default.css` when no theme is active). It contains only `:root { --variable: value; }` declarations from validated values, so there is nothing to escape beyond hex colors, catalogue font strings, a hash-based banner URL and two numbers. It is cached for a year because the URL changes whenever the theme or the code changes.
- The HTML itself must not be cached at the edge for longer than a theme change should take to appear. Cloudflare does not cache `/events`, `/feedback` or `/api/kingdom-branding` today (checked 2026-10-02, §71.14); any future cache rule for them must bypass or vary (§72.3).
- `GET /events?preview_theme={id}` renders that theme for a signed-in superadmin; for anyone else the parameter is ignored. Previews send `Cache-Control: no-store` and `X-Robots-Tag: noindex`. The admin editor shows the real page in an iframe from this URL, so the preview cannot drift from the page. No `X-Frame-Options` or CSP header was seen on these pages (§71.14), so the iframe is expected to work; confirm with a `GET` on the first build.

### 71.7 Admin

A new **Appearance** tab (superadmin only) with four panels:
1. **Themes.** List with a swatch, fonts and an in-use marker. The editor has color pickers with live contrast ratios, font selects that load a family only when chosen and show sample text, a banner upload with a crop preview of the hero, the overlay slider, a copy-overrides section, "Start from a template", and the iframe preview. Save is disabled while any rule fails.
2. **Schedule.** The base theme select and the scheduled windows table, with the §64.1 shortcuts and a warning when windows overlap at the same priority.
3. **Page text.** The Kingdom's values for every registry key, a tab per enabled language (§72.7), the shipped text as the placeholder, and a link to preview the page.
4. **Kingdom branding.** The existing Kingdom color and titles move here from the Setup modal; the modal keeps name and slug.

**Permissions.** Superadmin only (decided 2026-10-02), matching Kingdom titles and colors today. The Appearance tab is hidden from everyone else and every endpoint uses `require_superadmin`. Opening it to Kingdom coordinators later is a permission change only. Every write goes through `services/audit.log_change` (asset rows log metadata, never bytes).

### 71.8 API

All under `/admin/api`, Kingdom-scoped by `kingdom_id`:
- `GET/POST/PATCH/DELETE /themes`, `GET /theme-templates`, `GET /fonts` (the catalogue)
- `POST /theme-assets`, `DELETE /theme-assets/{id}` (only when unreferenced)
- `GET/POST/PATCH/DELETE /scheduled-themes`
- `GET/PUT /page-copy` (Kingdom values)
- `PATCH /kingdoms/{id}` gains `default_theme_id`

Public, unauthenticated: `GET /theme/{id}.css`, `GET /theme-assets/{sha256}.webp`. Neither returns anything about an unscheduled theme beyond what its own stylesheet contains. `/api/kingdom-branding` keeps its current fields for the admin console and gains none.

### 71.9 Schema change (revision `a1f0c0de0008`)

Additive, after the language revision `a1f0c0de0007` of §72.13 (which creates `kingdoms.page_copy`). New tables `themes`, `theme_assets`, `scheduled_themes`; new column `kingdoms.default_theme_id` (nullable FK to `themes`, `ON DELETE RESTRICT`; created after `themes`). Named constraints so `db_errors.py` can translate them. The downgrade drops them. A code-only rollback is safe because nothing reads the new columns when the code is old. The migration needs the usual PG test under `SAMAYA_MIGRATION_TEST_PG`.

### 71.10 Failure behavior

| Case | Result |
|---|---|
| No themes, no copy set | Pages render exactly as today. |
| Theme row fails validation at render (hand-edited data) | Log, use the shipped defaults. |
| Font key not in the catalogue | Default font for that slot, warning logged. |
| Banner asset missing or the image fails to load | The hero gradient, no layout shift. |
| Copy value over its limit or unknown key | Rejected on save; ignored at render. |
| Google Fonts unreachable | System fallback stack. |

### 71.11 Tests

Contrast rules (each accept and reject case, shared fixtures with the frontend), overlay worst-case math, theme CRUD, permissions (superadmin allowed; coordinator, owner and viewer refused), copy resolution order and escaping, HTML marker injection and the JSON block's escaping (`</script>` in a value), resolution with overlapping windows and equal priorities, 409 on deleting a used theme, image processing for every profile (§71.4: size, pixel bomb, animation to a still, wrong type, SVG, EXIF dropped and orientation applied, alpha flattened for covers, icon cropped square, banner width cap, re-processing is stable) and the PATCH semantics of covers and icons, i18n catalogue parity and locale choice (§72.5), asset serving headers, stylesheet content, preview parameter ignored for anonymous users, the §64.1 window functions, the 0007 migration upgrade and downgrade on Postgres.

### 71.12 Build order

Each step is deployable and leaves the site working. §72.14 holds the language steps; together:
1. **Images.** `services/images.py` with Pillow, applied to event covers and alliance icons; `reprocess_images.py`. No theme code needed, and it shrinks what the admin and the Discord engine carry today.
2. **Language foundation** (§72.14 step 1), English only.
3. **Languages and page text** (§72.14 step 2, with §71.3's editor, server-side head and JSON injection, feedback page wiring).
4. **Themes, fonts and schedule** (tables, editor, catalogue, generated stylesheet, base theme, scheduled windows, preview).
5. **Banner images** (assets table, upload, serving, hero rendering).

### 71.13 Out of scope

Per-alliance themes (an alliance's color still tints the page while it is selected, §63.2), dark mode, free-form CSS, uploaded font files, per-visitor theme choice, animated or video banners, themes for the admin console, and self-hosted fonts (a later per-font change). Translated page text is in scope (§72); translated event content is not (§72.9).

### 71.14 Decisions

Decided 2026-10-02:
1. **Who edits:** superadmin only.
2. **Numerals font:** restricted to monospace and verified tabular families, so times stay aligned.
3. **Base theme:** a selectable setting, `kingdoms.default_theme_id`, configured in the Schedule panel.
4. **Font source:** Google Fonts.
5. **Pillow:** added now and used for Discord event covers and alliance icons as well as banners (§71.4).
6. **Languages:** the interface becomes multilingual soon; page text is stored per language (§72).

Pre-deploy checks, made 2026-10-02 from `lxc-taraka` and from the owner's knowledge of the Cloudflare account:
- **Caching.** `/events`, `/feedback` and `/api/kingdom-branding` return `cf-cache-status: DYNAMIC`; `/static/events.css` is cached (`max-age=14400`). The owner has configured no Cloudflare cache rules. HTML and JSON are therefore not cached at the edge, so a page-text or theme change shows on the next load. The new `/theme/*.css` and `/theme-assets/*.webp` URLs end in cacheable extensions and are cached by default, which their immutable hashed URLs want.
- **Framing.** No `X-Frame-Options` and no `Content-Security-Policy` header came back for those URLs, so the same-origin preview iframe is expected to work. These were `HEAD` requests; the check is repeated with `GET` on the first preview build.

### 71.15 Header art, extended tokens and accessibility (merged from the Kingshot Themes handoff, 2026-10-02)

Added after reviewing `THEME-SPEC.md` from the design handoff. Its file-based theme model (CSS blocks per theme, images under `/static/themes/`, a `localStorage` theme cache, `check_themes.py`) is not adopted; §71 stays the model. What is merged:

- **Header art.** Two optional images per theme, `header_asset_id` (desktop, exactly 1920 by 400, at most 150 KB) and `header_mobile_asset_id` (900 by 500, at most 80 KB, used at 640 px and narrower). Painted by `body::before` (events and feedback pages), height `--art-h` (default 300 px, 240 px on phones), faded out with a mask over the existing gradient fallback, fading in over 200 ms (none under reduced motion), never preloaded. When a theme has header art the title block sits on a white plate (`--plate-bg`, `--plate-pad`). Cards, rows, days and the calendar stay opaque.
- **Images.** Profiles `theme_header`, `theme_header_mobile` and the reshaped `theme_banner` (1200 by 360, at most 100 KB) fill and center-crop to the exact size, refuse a source smaller than the target (never upscaled), encode WebP and lower the quality until the budget fits (floor 40, else a 422 asking for a simpler image). Each upload requires a `credit` (source and license, 3 to 300 characters), stored on `theme_assets.credit`. A theme must use an asset of the right size for the slot. Replacing or removing an image, or deleting a theme, deletes images no theme uses; an image in use cannot be deleted (409). Audit rows for assets hold metadata only.
- **Extended tokens.** `hero_wash` (color; `banner_overlay` stays its strength, 0.90 to 1.00), `tint` (optional hover tint), `radius` (4 to 14 px) and `art_height` (240 to 360 px); null means the shipped value. Surface and border colors stay derived or shipped.
- **Wider contrast rules.** Accent text on the table-band surface (`#F8FAFD`) and over a black banner under the wash; body and secondary text over a black banner under the chosen wash color and strength (the worst case any image can produce); body and secondary text on the hover tint. Same shipped `ink` and `muted` as §71.1.
- **Accessibility.** Under `forced-colors: active` and `prefers-contrast: more` the art, banner and plate are removed (plain CSS).
- **Revision.** The additions are part of revision `a1f0c0de0008` (not yet released, so edited in place): `theme_assets.credit`; `themes.header_asset_id`, `header_mobile_asset_id`, `hero_wash`, `tint`, `radius`, `art_height`; CHECKs `ck_theme_radius`, `ck_theme_art_height`. Verified on Postgres: upgrade, `alembic check`, downgrade, upgrade.
- **Admin.** The theme editor gains the new controls and an image slot per image (thumbnail, file, credit, remove), with the contrast list mirroring the server rules.
- **Standard look.** A footer button "Use the standard look" (events footer, feedback page), shown only while a theme is active, sets the cookie `samaya_standard=1`; the server then renders without the theme, so nothing flashes, and the page offers "Use the themed look" to undo it. The admin preview ignores the cookie. `static/look.js`, `data-theme-state` on `<html>`, strings `public.common.standardLook` and `themedLook` in all eight languages (drafts).
- **Not yet built.** Page text overrides (`theme.copy`).

## 72. Interface Languages

**Status:** Designed 2026-10-02. Built so far: §72.14 step 1, the English-only foundation for the public pages, and step 2 for the public pages (2026-10-02): revision 0007 (`kingdoms.default_locale`, `enabled_locales`; `page_copy` is left to the theme step because it has no editor yet), the Kingdom language settings in the admin Kingdom modal, the language select, logical CSS, `<bdi>` and Unicode isolates for names, locale-aware first weekday (English keeps Monday), script fonts, and draft translations in all seven other launch languages (not yet native-reviewed, so none is enabled until the superadmin enables it) (`services/i18n.py`, `services/public_pages.py`, `static/i18n.js`, `app/i18n/en.json`, the `en-XA` pseudo-locale, `Intl` formatting). Not built: page text per language (§71.3), native review of the translations, the admin console in other languages, and Discord. The first part (the foundation and the public pages) comes before the theme work of §71.

**Problem.** The Kingshot community is international, and every label on the public pages is English text written into `events.html`, `events-public.js` and `feedback.html`. Leaders want the pages, and then the Discord commands and the admin console, in other languages, and soon. The text editing of §71.3 must not be designed for one language and migrated later.

### 72.1 Scope

1. **Public pages** (`/events`, `/events/{slug}`, `/feedback`): in the first release.
2. **Admin console:** second, with the same mechanism (§72.10).
3. **Discord slash command replies and command names** (§70): last, handled later (decided 2026-10-02). Until then Discord replies stay English. The `t()` catalogue is shared, so the `discord.*` keys are added when this phase starts, not before.

Not translated: event names, descriptions and messages, message templates, alliance and Kingdom names. Leaders write those in whatever language they choose. Translating them is a separate feature (§72.9).

### 72.2 Locales

A locale is a BCP 47 tag (`en`, `tr`, `ko`, `es`, `pt-BR`, `de`, `fr`, `ru`, `ar`, `zh-Hans`). A shipped locale is a file in `app/i18n/<locale>.json`. Each file has a `_meta` object: `name` (the language in its own script), `dir` (`ltr` or `rtl`), `script`, and `reviewed` (a native speaker has confirmed the text). Matching is exact, then language only (`fr-CA` falls back to `fr`, `ar-EG` to `ar`), then the Kingdom default. The Kingdom has `default_locale` (default `en`) and `enabled_locales` (a list). Only enabled locales are offered or matched. Chinese is script-aware: `zh`, `zh-CN`, `zh-SG` and `zh-Hans` match `zh-Hans`, while `zh-TW`, `zh-HK` and `zh-Hant` do not match it (a Traditional-script reader should not silently get Simplified) and fall to the Kingdom default until a `zh-Hant` locale exists.

**Launch languages (decided 2026-10-02).** English, Simplified Chinese, Modern Standard Arabic, French, Spanish, Turkish, Russian and German:

| Tag | Native name | `dir` | Script | CLDR plural categories | Fallback font for the script |
|---|---|---|---|---|---|
| `en` | English | ltr | Latin | one, other | none (Noto Sans) |
| `zh-Hans` | 简体中文 | ltr | Han | other | Noto Sans SC |
| `ar` | العربية (Modern Standard Arabic) | rtl | Arabic | zero, one, two, few, many, other | Noto Sans Arabic |
| `fr` | Français | ltr | Latin | one, many, other | none |
| `es` | Español | ltr | Latin | one, many, other | none |
| `tr` | Türkçe | ltr | Latin | one, other | none |
| `ru` | Русский | ltr | Cyrillic | one, few, many, other | none (Noto Sans covers Cyrillic) |
| `de` | Deutsch | ltr | Latin | one, other | none |

`ar` text is written in Modern Standard Arabic, not a regional dialect, so one file serves every Arabic-speaking region. Of these, Arabic (right to left, six plural forms, a different script) and Chinese (a large glyph set, no plurals, no spaces between words) are the two that change code, not only text.

### 72.3 Choosing the visitor's language

In order: a valid `?lang=` parameter (so a shared link opens in a chosen language), the `samaya_lang` cookie (set by the language select; functional, holds only the tag, one year), the browser's `Accept-Language` best match among enabled locales, then the Kingdom default. A language select appears in the header only when more than one locale is enabled. The server sets `<html lang dir>`, the `<title>`, description and Open Graph tags, and `hreflang` alternates, so a link pasted into Discord previews in the language of the link. The HTML varies by `?lang`, cookie and `Accept-Language`; Cloudflare does not cache these pages today (checked 2026-10-02), and any future cache rule for them must vary on all three or bypass.

### 72.4 Catalogue and runtime

`app/i18n/en.json` is canonical. Keys are flat and grouped by prefix: `public.*`, `discord.*`, later `admin.*`. A value is a string, or an object of CLDR plural categories (`zero`, `one`, `two`, `few`, `many`, `other`) for counted text. Placeholders are `{name}`. Lookup for one key: the locale, its language, the Kingdom default, then `en`.

- **Server:** `services/i18n.py` with `t(locale, key, **params)`, used by the page injection and by the Discord handlers, so one catalogue serves both. Plural categories on the server need CLDR rules, which Python does not ship; the options are the `Babel` library (a new dependency) or writing `discord.*` strings without counts. To decide when the Discord step starts.
- **Browser:** the server merges the lookup chain for the page's keys and writes it into the HTML as `<script type="application/json" id="i18n">` (with `<` escaped), next to the page-text block of §71.3. A small `t(key, params)` reads it, using `Intl.PluralRules` for plurals. No separate fetch, so no flash of English. Elements carry `data-i18n` (text) and `data-i18n-attr` (for `aria-label`, `title`, `placeholder`), applied at start-up before first paint. The static markup keeps its English text, so the page reads correctly if the block is missing.
- **Dates, numbers, durations:** `Intl` with the selected locale (today the page passes `undefined`, the browser's language). Relative times and durations that are hand-built English phrases now ("starts in 3h", "ends in 30 min", "2 days") move to `Intl.RelativeTimeFormat` and `Intl.NumberFormat` with `unit`, and lists to `Intl.ListFormat`. The 24-hour clock and the UTC column stay. In English a few phrases will read slightly differently ("in 3 hours" for "in 3h").
- **Digits:** times and the countdown always use Western digits (`numberingSystem: latn`), in every locale, so the UTC column, the tabular alignment of §71.2 and screenshots agree.

### 72.5 Extraction and checks

Every user-visible literal in `events.html`, `events-public.js`, `feedback.html`, `feedback.js` and `services/discord_commands.py` moves into the catalogue (roughly 150 strings for the public pages, an estimate). Tests, in `tests/test_i18n.py` and the vitest suite:
- every key used in code exists in `en.json`, and no `en.json` key is unused;
- every locale file has no unknown keys, the same placeholders as `en` for each key, and only the plural categories its language uses;
- a pseudo-locale, `en-XA`, generated from `en.json` (accented letters, about 40 percent longer, bracketed), is used in development and by a Playwright smoke test to expose hard-coded English and overflow. It is never shipped or enabled.

### 72.6 Layout, scripts and fonts

- **Right-to-left (required at launch, because `ar` is a launch language).** `dir` comes from the locale. `events.css` and `feedback.css` move from physical properties (`margin-left`, `left`, `text-align: left`) to logical ones (`margin-inline-start`, `inset-inline-start`, `text-align: start`), and the calendar grid, the hero's progress bar, chevrons and menus are checked in a mirrored layout (`ar` and the `en-XA` mirror variant). Icons and arrows that carry direction (back link, chevrons) mirror; clocks and the progress bar do not need to, but the bar fills from the inline start.
- **Mixed-direction text.** Alliance names, event names and Kingdom names are written by leaders in any language and sit inside sentences. In HTML every interpolated name goes in a `<bdi>` element, and free text uses `dir="auto"`, so an English event name in an Arabic sentence (or the reverse) keeps its own punctuation and order. In Discord replies the same names are wrapped in Unicode isolates (U+2068 and U+2069) when the reply locale is right to left.
- **Calendar week.** The first weekday follows the locale (`Intl.Locale.prototype.getWeekInfo` where the browser has it, otherwise Monday), because Arabic regions and the US start the week on different days than Europe. To check against the calendar code when the step is built.
- **Length.** Labels must not clip at the longest languages (German and Russian run 30 to 40 percent longer than English). The `en-XA` smoke test at 360 px is the check.
- **Fonts.** A theme's fonts must cover the visitor's script. Catalogue entries list their scripts (§71.2). For a locale whose script a chosen font lacks, the generated stylesheet appends the catalogue's fallback family for that script to that page's stack: `Noto Sans SC` for `zh-Hans` and `Noto Sans Arabic` for `ar`, loaded in script chunks (Google splits the Chinese set into many small slices), so only those visitors download them. A Latin-only heading face such as Cinzel renders Russian, Chinese and Arabic headings in the fallback font; the Languages panel warns about that for the base theme. Enabling a locale whose script the base theme's fonts do not cover shows a warning in the admin.

### 72.7 Page text per language

§71.3 stores and resolves page text per language as described there. In the admin each page-text field has a tab per enabled language, with the shipped text as the placeholder.

### 72.8 Discord

- **Replies.** The interaction payload carries the asker's `locale`, which uses Discord's own tags. A reply uses it if it maps to an enabled locale, then the payload's `guild_locale`, then the Kingdom default. Each asker therefore gets their own language. Event times are already localised by Discord's `<t:...>` tokens. Mapping: `en-US` and `en-GB` to `en`; `zh-CN` to `zh-Hans` (`zh-TW` has no match, as in §72.2); `es-ES` and `es-419` to `es`; `fr`, `de`, `tr` and `ru` to themselves.
- **Arabic caveat.** To the author's knowledge Discord's client has no Arabic interface language, so the payload `locale` of an Arabic speaker is never `ar` (usually `en-US`), and Discord accepts `name_localizations` only for its own list of locales. Arabic would therefore not appear in command names or in replies chosen by the asker's locale. Two possible remedies, neither designed: a reply language set per Discord server (`discord_servers.locale`, used when the asker's locale does not map to an enabled locale), and an `ar` option on the commands. To verify against Discord's current locale list when the Discord step starts.
- **Command names.** `app/register_discord_commands.py` sends `name_localizations` and `description_localizations` from the catalogue (`discord.cmd.*`) for each enabled locale. Discord restricts command names (lowercase, 1 to 32 characters, limited character classes), so a locale whose translated name fails the check keeps the English name; the registration dry run lists those.
- **Feedback modal.** Title, field labels and the confirmation come from the catalogue.

### 72.9 Not in the first release: translated content

Leaders' own words are the other half of a multilingual Kingdom. Two possible designs, neither started: per-language text on an event (name, description, message), shown on the public page by the visitor's language; and a language on each Audience destination (§68), so a Discord message or Scheduled Event goes out in the language of that server. The second needs the language of the post to be chosen per destination, which touches the delivery engine's merge rule (§67), so it needs its own design.

### 72.10 Admin console

The same catalogue under `admin.*`. Each user gets a language (`users.locale`, in `/api/me` and settable on the Setup tab). Strings in `admin.html` and `js/*.js` are extracted the same way (several hundred, an estimate). Error text from the API (`detail` strings written in English in the routers) is not translated at first; localising it needs error codes, which is its own change.

### 72.11 Admin settings

In the Appearance tab (superadmin only), a **Languages** panel: one row per shipped locale with its native name, its `reviewed` flag and a script-coverage warning; checkboxes to enable; the default language. Enabling an unreviewed locale is allowed and the panel says so.

### 72.12 Translation workflow

The catalogue files are in the repository and change by pull request. First drafts may be machine-assisted (Claude can draft them) and stay `reviewed: false` until a native speaker from the community confirms them. CI enforces the parity checks of §72.5. If community translators need an in-app editor, database overrides on top of the files are a later addition and are not designed here.

### 72.13 Schema (revision `a1f0c0de0007`)

Additive: `kingdoms.default_locale` (text, not null, default `en`), `kingdoms.enabled_locales` (JSON text, default `["en"]`), `kingdoms.page_copy` (JSON text, nullable; shape in §71.3). Later, with the admin console, `users.locale`. The downgrade drops the columns. A code-only rollback is safe.

### 72.14 Build order

1. **Foundation, English only.** The catalogue and `t()`, extraction of the public pages, locale selection and `<html lang dir>`, the `i18n` JSON block, `Intl` formatting, `en-XA`, tests. Nothing changes for visitors except the small wording shifts of §72.4.
Step 1 notes (built 2026-10-02): values sent to the API stay English whatever the page language (the report and feedback `error_type` options keep English `value` attributes, and the "Proposed name:" lines of a request), because admins read them and the server validates them. The calendar still starts its week on Monday in every locale, and names are not yet wrapped in `<bdi>`; both belong to step 2 with the logical-CSS work. The `en-XA` smoke test found one existing layout bug, the Add to calendar menu running off the left edge of a 390 px screen in English too; fixed in the same change.

2. **Languages and page text.** Revision 0007, the page-text editor with a tab per language, the Languages panel, the language select, the logical-CSS and bidi work (needed for `ar`), the launch translations (seven languages besides English, about 150 strings each, an estimate; public pages only). Languages are enabled one at a time as each is reviewed, so the step can ship with English and the first reviewed languages and add the rest without a deploy.
3. Then §71's theme, font, schedule and banner steps (§71.12).
4. **Admin console** translation (`admin.*` keys), next after the public pages.
5. **Discord** localisation (§72.8), last: replies, `name_localizations`, and the Arabic remedy.

### 72.15 Decisions

Decided 2026-10-02: plan for multilingual UI soon; per-language page text from the start (not one language and a later migration); the launch languages are English, Simplified Chinese, Modern Standard Arabic, French, Spanish, Turkish, Russian and German (§72.2).

Decided 2026-10-02: scope order is public pages, then admin pages, then Discord (§72.1). The Arabic-in-Discord question (below) waits for the Discord phase.

Decided 2026-10-02, accepting the recommended defaults: (1) Claude drafts translations and community natives review them, with `reviewed` shown in the admin only, and Arabic and Chinese get a native read before they are enabled; (2) Western digits for times and counts in every language (§72.4); (3) `?lang=` plus cookie, no path prefix (§72.3); (4) the `Babel` library for server plural rules (§72.5).

No open decisions remain. Build starts with §71.12 step 1 (images).

## 73. Subscribe links and the Week / 3-day calendar

Built 2026-10-02 (revision: no schema change).

### 73.1 Alliance chips

The chips on `/events` and `/events/{slug}` are ordinary links (`/events`, `/events/{slug}`). A plain click navigates. The earlier in-place filter on the combined page is removed: the alliance pages already show that alliance plus Kingdom-wide events, and the URL is what a visitor needs to subscribe to one alliance's feed.

### 73.2 Subscribe links, checked against vendor documentation

Only the manual paths are documented by the vendors. The one-click deep links are conventions that work in practice but could change without notice, so the menu always offers the copyable feed address next to them.

| Provider | What the vendor documents | What the menu does |
| --- | --- | --- |
| Google Calendar | "Add other calendars, From URL" (support.google.com/calendar/answer/37100). The `?cid=` deep link is not documented | `https://calendar.google.com/calendar/r?cid=` plus the **webcal://** feed, percent-encoded. An `https://` value in `cid` is rejected with "Unable to add calendar" (community reports; the previous link used https). Google fetches a webcal feed over http first. Production answers the feed directly on http (no redirect) and Google subscribes successfully, so an https redirect is not required |
| Apple Calendar | "New Calendar Subscription"; clicking a link from a web page or email subscribes (support.apple.com/guide/calendar/icl1022/mac) | `webcal://` link |
| Outlook.com | "Subscribe from web" (support.microsoft.com, "Import or subscribe to a calendar in Outlook.com or Outlook on the web"). `addfromweb` is not documented | `https://outlook.live.com/calendar/0/addfromweb?url=<https feed>&name=<title>`. Personal accounts only |
| Outlook desktop | Account Settings, Internet Calendars. Microsoft documents that Outlook desktop can silently fail to add a feed when the server mishandles its modern-authentication probe (learn.microsoft.com, "Can't add an Internet calendar") | No one-click link; "Copy feed link" is the path. The Apple item no longer claims Outlook desktop |
| Any other app | Subscribe by URL | "Copy feed link" and "Download .ics file" |

Refresh is the provider's choice and cannot be forced: Google about 12 to 24 hours, Outlook.com about 3 hours (Microsoft states up to 24), Apple per the user's Auto-refresh setting. `REFRESH-INTERVAL` and `X-PUBLISHED-TTL` are sent as hints only.

Feed changes: the invalid `CALNAME` property is no longer emitted (`X-WR-CALNAME` stays), and both feeds answer `HEAD`. `DTSTAMP` stays the generation time on purpose: a stale value could make Apple ignore a moved occurrence.

Verified in production on 2026-10-02: the Google link subscribes and the events appear. Not yet checked: the Apple, Outlook.com and Outlook desktop paths. The vendors' deep links are conventions and can change without notice, so re-check them if a report comes in.

Incident, 2026-10-02: the first production check returned `200` with `Content-Length: 0` for every `.ics` URL, whatever the user agent or query string, while the app served the full feed (87 events) on `127.0.0.1:8000`. Neither Cloudflare nor the tunnel was involved. The Caddyfile on `lxc-taraka` is a path allowlist and had no line for `/events.ics`, `/events/*`, `/theme/*` or `/theme-assets/*`, so Caddy answered them itself with an empty 200. Those four lines were added and Caddy was restarted. Rule for the future: a new public route is not finished until it returns its real body through `https://ks138.taraka.dev` (README "Adding a new public route").

### 73.3 Week and 3-day views

The Calendar tab has a Month / Week / 3 days switcher (stored in `samaya_cal_mode`). Each day is a 00:00 to 24:00 column, 56 px per hour, with hour, half-hour and quarter-hour lines. The gutter shows the display zone and, when it is not UTC, UTC beside it. Overlapping events share lanes; events that cover a whole day go to a sticky all-day strip; announcements get a 30 minute block; a red line marks now. Previous and Next move 7 or 3 days; Week starts on the language's first weekday (`SamayaI18n.firstWeekday()`). Clicking a day heading or an event selects that day and opens its row in the list.

Implementation rules that differ from the design handoff: geometry is passed as custom properties through `data-vars` (no inline `style=`), CSS uses logical properties so Arabic mirrors, time ranges are wrapped in `<bdi dir="ltr">`, and the pure geometry (`dayBlocks`, `weekStartOf`, `addDays`, `minsOf`) is unit tested.

Known limit: a day is always 24 rows, so on a daylight-saving change day the hour rows are off by one after the change.

## 74. Canonical public URLs

Built 2026-10-02. The previous scheme put alliance pages under `/t/{slug}/...` and the all-alliance versions at the root, so the two looked unrelated. This was changed during the testing phase, before adoption, with no redirects: the `/t/...` and `/ics/events.ics` paths now return 404.

| What | All alliances | One alliance |
| --- | --- | --- |
| Page | `/events` | `/events/{slug}` |
| Data | `/api/events` | `/api/events/{slug}` |
| Last activity | `/api/last-activity` | `/api/last-activity/{slug}` |
| Calendar feed | `/events.ics` | `/events/{slug}.ics` |

Part II sections written before this change still name the old paths; they record the design at the time.

Rules: the feed is the page URL plus `.ics`; an unknown slug is a 404 on every path; the ICS router is registered before the events router so `/events/{slug}.ics` is never read as a page for a slug ending in `.ics`; an alliance slug is validated on create and edit to one path segment (lowercase letters, digits and hyphens, 1 to 32 characters, no dot or slash), so it can never break either route. Existing slugs are not rewritten.

Consequences for operators: anything outside the repository that names the old paths must change (Cloudflare WAF, cache or Access rules written for `/t/*`, bookmarks, Discord messages, calendar subscriptions). ICS event UIDs are unchanged, but a calendar app that subscribed to the old feed URL gets a 404 and must re-subscribe.

## 75. Several live events in the hero

Built 2026-10-02 from the "multi-live hero" design handoff.

- No live event, or one: unchanged (a single "Live now" or "Next up" card).
- Two or more live events: the hero becomes a stacked block. The first card is labelled "Live now · N events", the second "Also live"; each has its own countdown and progress bar. The first two are the ones that started earliest. Announcements never count as live.
- More than two: a "+N more live · see all" link switches to the List view and scrolls to the schedule. The design handoff also cleared an alliance filter there; the page no longer has one (§73.1), so the link only changes the view.
- "Then" lists upcoming events only, as before.
- Countdowns and bars are found by class (`js-cd`, `js-bar`) instead of by id, so every card ticks.
- Strings: `heroLiveCount` (plural, per language), `heroAlsoLive`, `heroMoreLive`, in all eight languages. The "more live" strings avoid verb agreement so they read correctly for any count.

## 76. Ticket board (fixed columns)

Requirement recorded 2026-10-03, adapted from the "Local-First Kanban" PRD and schema to this stack. The board is for developing Samaya only: it manages the feedback and issue tickets users submit (§66.10). It holds no personal data beyond the existing optional `submitter_contact`, which stays admin-only.

### 76.1 Decisions

- The board is a **view over `tickets`**. There are no `boards`, `cards` or `swimlanes` tables, no SQLite, and ids stay integers, so backups, CI, the audit log and the public API are unchanged.
- **Columns are the statuses and are fixed:** Open, Planned, In progress, Done, Declined. `dismissed` is not a column; it stays an explicit action and stays visible in the List view.
- A status is a public decision (§66.10): Open, Planned and In progress show on the public board, Done and Declined show in its read-only archive and close voting. Moving a ticket into Done or Declined therefore asks for confirmation.
- **Order within a column** is `tickets.position`, a nullable integer. Positioned tickets sort first by `position`; the rest follow by upvotes, then newest. A move rewrites the positions of the whole destination column (0, 1, 2, ...), so there are no gaps or fractions to maintain. A status change made any other way (the status select, restore) clears `position`, so the ticket joins the unpositioned tail.
- Concurrency is last write wins. Two admins moving cards at once can overwrite each other's order, which is acceptable for a handful of admins.

### 76.2 Phases

| Phase | Content | State |
| --- | --- | --- |
| 1 | `position`, `POST /admin/api/tickets/{id}/move`, Board view in the Feedback tab | Built 2026-10-03 |
| 2 | Checklists (one level), internal notes, color tags with a tag filter, JSON and Markdown export | Built 2026-10-03 (§76.5) |
| 3 | WIP limit per column, enforced by the move endpoint (409 with an override flag) | Built 2026-10-03 (§76.6) |

### 76.3 Phase 1 as built

- Migration `a1f0c0de0009`: nullable `tickets.position`. Additive; the downgrade drops the column.
- `POST /admin/api/tickets/{id}/move` with `{status, index}`: `status` must be one of the five column statuses (`dismissed` is rejected with 422), and `index` is the 0-based place in the destination column, counted without the moved ticket and clamped to its length. A dismissed ticket answers 409 until restored. Viewers get 403. The move is written to the audit log with before and after `status` and `position`. The response holds the updated ticket and the new `positions` of the destination column.
- `GET /admin/api/tickets` now includes `position`. The public endpoints never return it.
- Ordering and placement are pure functions in `services/ticket_board.py` (`ordered`, `place`).
- UI: a List / Board switch in the Feedback tab (stored in `localStorage` as `samaya_ticket_view`). Each card has a "Move" select (every other column, plus Move up and Move down) that works with keyboard and touch. Mouse users can also drag a card to a column or between cards; HTML5 drag-and-drop does not work on touch screens, which is why the select exists. The UI updates at once and rolls back with a toast if the server refuses.

### 76.4 Not delivered from the PRD

| PRD item | Why |
| --- | --- |
| Local-first sync (offline edits, merge) | Needs a sync engine with conflict resolution. The UI updates optimistically instead, and changes made offline are lost. |
| React and Astro | The repo has no build step. The features are plain scripts. |
| Custom columns (create, rename, reorder) | Decided against: statuses drive public visibility, so columns stay fixed. |
| SQLite, UUID keys | The board uses the existing Postgres tables and integer ids. |
| No-auth deployment behind Cloudflare Access | Samaya keeps Discord login and roles; the move endpoint uses `require_not_viewer`. |
| CI webhooks on column moves, local-LLM helpers | Phase 2 of the PRD; no outbound webhook path exists. |

### 76.5 Phase 2: checklists, internal notes, tags, export

Everything here is admin-only. No public endpoint returns a checklist, a note, a tag or `position`; `test_ticket_work.py` asserts it.

- **Checklist (one level).** `ticket_checklist_items` (`ticket_id`, `body` 1 to 200 characters, `done`, `position`). At most 50 items per ticket, listed in the order they were added. Add, edit the text, tick and delete; there is no nesting and no reordering. The ticket shows "done of total" on its board card.
- **Internal notes.** `tickets.internal_notes`, text up to 5,000 characters, saved through the existing `PATCH /admin/api/tickets/{id}` and included in its audit snapshot. Notes are Markdown. The admin renders them with a small in-house renderer that escapes all HTML first and then applies a fixed subset (headings, bold, italic, inline and fenced code, bullet and numbered lists, `http(s)` links), so there is no vendored library and no way to inject markup. Public ticket text and team replies stay plain text.
- **Tags.** `ticket_tags` (`name` up to 24 characters, unique ignoring case, `color` hex) and `ticket_tag_links`. Tags are shared by every admin because tickets are Kingdom-wide, and they are internal labels: `kind` stays the public category. At most 8 tags per ticket. The label text color comes from `services/contrast.pick_ink`. Any admin who is not a viewer can create, rename, recolor and delete tags; every write is audited, and deleting a tag records how many tickets it was on. `PUT /admin/api/tickets/{id}/tags` replaces a ticket's tag set. A tag filter in the Feedback tab applies to both List and Board.
- **Export.** `GET /admin/api/tickets/export?format=json|markdown&include_contact=0|1`. JSON is one file with every ticket, its notes, checklist, tags and public responses. Markdown is a zip with one `NNNN-slug.md` per ticket (front matter written with JSON-quoted values, so a hostile title cannot break the file). `submitter_contact` is left out unless `include_contact=1`. Dismissed tickets are included, marked by their status. Every export is written to the audit log as a `create` on the pseudo-table `ticket_exports` (format, whether contact was included, ticket count), because the audit table only allows create, update and delete.
- **Endpoints.** Checklist: `POST /tickets/{id}/checklist`, `PATCH` and `DELETE /tickets/{id}/checklist/{item_id}`. Tags: `GET/POST /ticket-tags`, `PATCH/DELETE /ticket-tags/{id}`. Viewers can read everything and change nothing. The admin ticket list now carries `internal_notes`, `checklist` and `tags` for each ticket.
- **Migration** `a1f0c0de0010`: the three tables and `tickets.internal_notes`. Additive; the downgrade drops them.

# Archive

Fully superseded designs, moved here unchanged except for position. Each begins with its own "Superseded" note. They stay for historical reasoning only and do not describe the current system.

## 14. Admin UI — Combined Multi-Tenant View

**Superseded by §38.** This section's design (a `tenantPicker` "All my alliances" option, a single global combined-mode flag alongside `getCurrentTenantSlug()`/`setCurrentTenantSlug()`, and Config/Access/Platform/Announcements staying single-tenant-only) was the first version of cross-alliance viewing. §38 replaced it outright: the header tenant picker was removed entirely, every combined-capable tab gained its own independent per-tab alliance filter (`getTabFilter`/`setTabFilter`, `COMBINED_SLUG = '*'`), and Announcements became combined-capable too (via `get_current_tenants` and its own `announcementsFilter`). See §38 for the current model; §14.4's rejection of merging tenants into one row, and its "writes stay single-tenant" principle, both still hold under §38's design.

## 18. Platform — Tenant Editing

**Superseded.** This section's `prompt()`-chain editor (pre-filled `name`/`slug`/`guild_id` prompts, a `bot_token` prompt with a `CLEAR` keyword) no longer exists in either shape it once had: §25 moved `guild_id`/`bot_token`/`public_key` off `Tenant` entirely onto `DiscordServer` (so a Tenant edit was never going to prompt for a bot token again), and §38.7 replaced the whole prompt-chain mechanism with a real **Tenant** modal (`platform.js`'s `openTenantModal()`/`saveTenantModal()`) — needed once the modal also had to handle a file-picker upload for the tenant's icon. See §25 for where bot credentials live now and §38.7 for the current Tenant create/edit UI.

## 36. Public Events Page: Off-Center Layout Fix

**Superseded by §62.** This was a PatternFly-specific CSS fix (`.pf-v6-c-page` grid-template-area quirk) for the old `events.html`. §62's redesign dropped PatternFly from the public events page entirely in favor of a standalone `events.css`, so the class this section patched no longer exists on that page and the underlying bug it fixed isn't applicable to the current layout. `admin.html` still uses PatternFly and still carries the equivalent fix for itself, unaffected by this.

### 36.1 Out of Scope

- Auditing every other PatternFly component on `admin.html` for the same grid-auto-placement class of bug — this fix addressed the one reported symptom (the page-level container) on the page that had it, not a general PatternFly-hardening pass. `events.html` no longer uses PatternFly at all (§62), so this bullet no longer applies there.

## 39. Public Status/Kind Legend

**Superseded by §62.** The always-visible legend card this section built (`#statusLegend`/`renderLegend()`, first thing in `<main>`, no dismiss control by design) is gone: §62's redesign replaced it with a collapsed-by-default "Key ▾" panel the visitor opens on demand. The underlying goal — explaining the kind badges and status vocabulary somewhere on the page — is still met by that panel; only the always-visible-card mechanism this section specified no longer exists.

### 39.1 Out of Scope

- A dismiss/collapse control for returning visitors — at the time, the legend was judged short enough not to need one. (Effectively superseded by §62, which made the legend a collapsed-by-default panel instead.)
- Per-alliance customization of the legend's wording — still true: the status vocabulary remains fixed and shared across every alliance.

## 47. Calendar View Legibility and Accessibility Pass

**Superseded by §62.** This section's fixes were all CSS/markup changes to the old PatternFly-based `events.html` (`.cal-item`, `.samaya-legend-kinds`, `.samaya-modal-close`, `buildEventCardHtml`'s cards) — none of those classes or functions exist in the page §62 replaced it with. The goals this section addressed (legible calendar items, a scannable legend, larger touch targets, full keyboard access) carried forward as requirements into §62's rewrite, which built its own accessibility pass (skip link, focus rings, `aria-expanded`/`aria-pressed`, Escape-to-close, `prefers-reduced-motion`, 44px touch targets) from scratch rather than reusing this section's specific fixes.

## 54. Legend Badge Truncation Fix

**Superseded by §62.** This was a CSS fix (`.samaya-legend-item`, PatternFly's `.pf-v6-c-label__text`) for the always-visible legend §39 built, which §62's redesign replaced with a collapsible "Key" panel using entirely new markup/CSS — the classes this fix touched no longer exist on the page.

## 58. Removed the "Today" Card's Blue Outline

**Superseded by §62.** This was a fix to PatternFly's `.pf-m-selected` modifier on the old `events.html`'s cards, replaced with a `.samaya-today-card` tint class. §62's redesign dropped PatternFly and that card markup entirely in favor of its own styling (the calendar view's current-day cell uses an `is-today` class); today's items are simply no longer marked up the way this section's fix touched.

### 76.6 Phase 3 as built

- Migration `a1f0c0de0011`: table `ticket_column_limits` (`status` primary key, `wip_limit` 1 to 999). No row means no limit. Additive; the downgrade drops the table.
- `GET /admin/api/ticket-board/limits` returns `{status: limit|null}` for the five columns (any admin). `PUT` sets or clears limits (`{limits: {status: int|null}}`, only the statuses present change; superadmin only, because a limit is team policy; audited as `update` on `ticket_column_limits` with the full before and after map).
- `POST /api/tickets/{id}/move` takes an optional `override` flag. A move into a different column that already holds its limit or more answers 409 unless `override` is true. A move inside the same column is never limited. An override that was actually needed is recorded as `wip_override: true` in the move's audit row.
- **Not enforced:** the status select in List view (`PATCH`) and Restore. The limit guards board moves only, as decided in §76.2. A column can also exceed its limit by other routes, such as a new ticket arriving as Open, so the header shows "N of L allowed, full" or "over the limit" in text, not only color.
- The board asks for confirmation before sending an override. The server stays the authority: a stale limit gives a 409 toast that says to reload.
- Admin UI: column headers show the count against the limit, and a superadmin gets a Column limits button in Board view. Limits never reach the public board or API.

## 77. A message per reminder

Requirement recorded 2026-10-03. An event's reminders can each carry their own text, for example "One hour left" at 60 minutes and "Starting now!" at 0.

### 77.1 Decisions

- `event_reminders.message` is a nullable column (migration `a1f0c0de0012`, additive; the downgrade drops it and reminders then use the event message again). NULL or blank means "use the event's message", so every existing event behaves as before.
- **Precedence when a reminder is sent:** the occurrence's `message_override`, then the reminder's own message, then the alliance's `message_override`, then the event's message, then the default reminder text (§77.4). A reminder's own message therefore applies to every alliance and replaces an alliance's own message for that reminder; an alliance override still applies to reminders without a message of their own.
- Placeholders (`{event_time_relative}` and the rest of §66.5) render in a reminder message like in any other message. Limit 2,000 characters, as for the event message.
- The Discord Scheduled Event description does not use reminder messages; it still follows occurrence override, alliance override, event message.
- Not built: event types' default reminders stay a plain list of minute offsets, so a new event does not inherit per-reminder messages from its type. Also not built: the composer toolbar and live preview per reminder (each reminder has a plain text box).
- Reminder messages are never part of a public payload.

### 77.2 As built

- API: `reminder_messages` is `{minutes: text}` on `POST /api/events` and `PATCH /api/events/{id}` (and the `changes` of the split endpoint), and in every event response (only reminders that have a message appear, keys are strings). On create and on patch a message must belong to a reminder in the resulting list (422 otherwise). On patch the map is a full replacement. Sending only `reminder_minutes` keeps the messages of the offsets that stay. Blank text drops the entry. Changes are audited in the event's `reminder_messages` snapshot. The split copies the messages to the new part of the series.
- Engine: `_send_reminder` looks up the delivery's own reminder row by `minutes_before`. Because deliveries merge by guild, channel and offset, one reminder message never conflicts with another.
- Admin: the Events form shows a text box under the reminder chips for each reminder (`createReminderEditor` with `withMessages`); the event type form is unchanged. Typed text survives adding other reminders and is dropped with a removed reminder.

### 77.4 Default reminder text

When a reminder has no message of its own and the event has none either, the text is built from the reminder's offset (changed 2026-10-03; it used to be "{name} starts {event_time_relative}"):

| Offset | Text |
| --- | --- |
| 0 | `Bear Hunt is starting now` |
| 1 | `1 minute until Bear Hunt` |
| 60 | `1 hour until Bear Hunt` |
| 90 | `90 minutes until Bear Hunt` |
| 2880 | `2 days until Bear Hunt` |

The largest whole unit is used (days, then hours, then minutes). The text has no Discord timestamp, so it reads correctly wherever the reminder is shown. `services/templates.default_reminder_text`; it is English only, like the rest of the posted messages. The event name is inserted as written and is not rendered as a template. A role mention is still added in front when the event asks for one.

### 77.3 Fix found on the way

An "at the start" (0 minute) reminder was always cancelled as "Event had already started": it comes due at the start time and the check was `start <= now`. It now gets a 5 minute grace period (`AT_START_GRACE`) so the one-minute tick, or a short outage, still posts it. Reminders before the start behave as before.

## 78. Installable app and push notifications

Requirement recorded 2026-10-04. Status: design only, nothing is built. Reviewed twice by independent reviewers before any code (78.18). The public events and feedback pages become an installable web app, and players can get push notifications for the events they choose, at lead times they choose, without an account.

### 78.1 Goal and non-goals

Goal: a player installs the schedule to their phone, picks what they care about once, and gets a notification at the times they asked for, even when Discord is muted.

Non-goals for this section: accounts or sync across devices, email or SMS, native apps, notifications for the admin console, notifications about feedback tickets (the board is for developing Samaya only), and replacing Discord reminders. Discord stays the primary channel and is unchanged.

### 78.2 Decisions

1. **Anonymous.** There are no accounts. A push subscription is the identity. Its preferences live on the server keyed by the subscription, and nothing else about the person is stored.
2. **One app, one service worker.** A single service worker registered at scope `/` serves both pages and holds the one push subscription. For `/admin`, `/auth`, `/invite`, `/webhooks`, `/health` and every non-GET request it does not call `respondWith` at all, so those requests never pass through it (78.6).
3. **Push follows the public schedule, not Discord.** What a subscriber can receive is exactly what the public query returns (`services/public_events`). Leadership-only, inactive and cancelled occurrences are never pushed. Discord Audiences, channels and roles do not apply.
4. **Players set their own lead times.** Push does not depend on an event's Discord reminder offsets, though "use each event's reminders" is the default mode (78.8).
5. **No quiet hours in v1.** Phones already have Do Not Disturb and Focus, and server-side quiet hours would need time zone handling (`tzdata` is not in the image) for a feature the OS does better (78.16).
6. **At most once, never late, never stale.** The engine claims a send before calling the push service, like Discord deliveries (§66.4). A push more than 15 minutes past its due time is skipped. A push whose due time is earlier than the moment the subscriber, the event or the occurrence last changed is skipped too (78.8). Discord reminders still send late.
7. **Standard Web Push through a service worker**, not Declarative Web Push, because it works across Chrome, Firefox and Safari (78.16).
8. **Wording is built on the device** from structured data and the current time with `Intl.RelativeTimeFormat` in the subscriber's language, so no per-language catalogue is needed for notification text. The §77.4 default reminder text stays English and Discord only.
9. **Management proof is the subscription's own auth secret.** The browser can always re-read it from its push subscription (78.11).
10. **Endpoint allowlist.** The server only sends to known push-service hosts (78.13). Without it, any visitor could make the server call any URL.
11. **A recurring event is followed by `series_id`, and sends are de-duplicated by `series_id` and date.** Occurrence ids are not stable: the engine deletes and recreates occurrences when an event is split, deactivated and reactivated or rescheduled (`_retire_occurrence`, `event_engine.py`). `series_id` survives all of these and is copied by a split.
12. **Off unless configured.** Push needs VAPID keys and `SAMAYA_PUSH_ENABLED=1`, read at call time. Otherwise the bell is hidden, the jobs are not registered and the API answers `enabled: false`.
13. **A separate scheduler job**, so a stall in Discord delivery cannot block push and the reverse.
14. **An async sender that the app controls.** `httpx` for transport, with `follow_redirects=False` and `trust_env=False`, and `http-ece` plus `py-vapid` (the libraries `pywebpush` itself builds on) for RFC 8291 encryption and the VAPID token. `pywebpush` is not used: it is synchronous (it would freeze the single event loop, which also runs the Discord tick and every web request) and its transport follows redirects and proxy environment variables by default (inference; check at build). The maintenance state and `cryptography` compatibility of `http-ece` and `py-vapid` are not verified and must be checked first (78.17 O5), with the RFC 8291 test vectors in the tests.

### 78.3 Facts the design relies on

| Fact | Status |
|---|---|
| A push service must accept a message of at least 4096 bytes (the encrypted body); larger may get 413. Encryption is RFC 8291 (`aes128gcm`) | Established (web.dev Web Push protocol guide, RFC 8030) |
| `TTL` is seconds the push service keeps an undelivered message and may be reduced by the service; `Urgency` is `very-low`, `low`, `normal` (default) or `high`; `Topic` lets a new message replace a pending one with the same topic (at most 32 characters of the URL-safe base64 alphabet) | Established (web.dev); the Topic length rule is from the reviewer's recall of RFC 8030, to be confirmed against the RFC |
| 404 means the subscription expired and 410 means it was unsubscribed; both mean delete it. 429 carries `Retry-After` | Established (web.dev) |
| VAPID: the JWT audience is the push service origin, expiry is at most 24 hours, and the subject is a `mailto:` or URL | Established (web.dev) |
| FCM answers 403 when a subscription was made with a different VAPID key, so a key rotation leaves dead rows that never return 404 or 410 | Reported (webpush-java issue 212); handled by the disable rule in 78.9 |
| iOS and iPadOS deliver web push only to web apps added to the Home Screen (display `standalone`), from iOS 16.4. Safari 18.4 adds Declarative Web Push for Home Screen web apps. macOS Safari has had web push since 16.1 | Established for Home Screen and 18.4 (WebKit blog, fetched); 16.4 and `standalone` reported by a reviewer from the WebKit iOS post, to be confirmed on a real device |
| Safari expects every push to end in a visible notification, so the service worker always calls `showNotification` | Widely held, not verified. The design always shows one |
| `Intl.RelativeTimeFormat` with `numeric: 'always'` gives "in 30 minutes" style text and `format(0, 'second')` with `numeric: 'auto'` gives the "now" wording in `en`, `fr`, `es`, `de`, `tr`, `ru`, `ar` and `zh-Hans`. `format(0, 'minute')` gives "this minute" and must not be used for the start | Verified by running Node on 2026-10-04 for all eight locales |
| The `/api/events` response was about 23 MB | Observed 2026-10-03. The cause in 78.5 is established by reading the code (every occurrence row carries the cover data URI), not yet by measuring a response |
| Behind Cloudflare, a client can send its own `X-Forwarded-For` and Cloudflare appends to it, so the leftmost entry that `rate_limit._client_ip` trusts is client-controlled. Whether Caddy overwrites the header, which would put every visitor in one bucket, is unknown | Plausible, unverified for this deployment (78.11, O9) |

### 78.4 Phases

| Phase | Delivers | Needs |
|---|---|---|
| 0 | Public payload diet: covers served by URL (78.5) | Owner decision (O2), migration `a1f0c0de0013`, Caddy line `/event-covers/*` |
| 1 | Installable app shell: manifest, icons, service worker, offline fallback (78.6) | Icons (O1), Caddy lines `/manifest.webmanifest` and `/sw.js` |
| 2 | Push core: subscribe, follow alliances and the Kingdom, the engine, the test button (78.7 to 78.11) | VAPID keys, migration `a1f0c0de0014`, the client IP fix (O9). No Caddy line: `/api/*` is already routed |
| 3 | Preferences: own lead times, type mutes, per-event follow and mute (78.8, 78.12) | Phase 2 |

Each phase ships and works alone. Rough effort in working sessions: phase 0 about 1 to 1.5, phase 1 about 1, phase 2 about 4, phase 3 about 1 to 2, so 7 to 8 in all, or 6 to 7 without phase 0.

### 78.5 Phase 0: public payload diet

`routers/events._row_dict` puts `event.cover_image_data`, a data URI, in every occurrence row, so a recurring event with a cover repeats the image once per occurrence in the 28-day window. That is the likely source of the 23 MB `/api/events` response. Stored covers are re-encoded by `services/images.py` (`EVENT_COVER`, 1600x800 JPEG), so each is typically a few hundred kilobytes as base64 (inference); the 8 MB figure in `validators.py` is the upload limit, not the stored size. Check on `lxc-taraka`:

    curl -s https://ks138.taraka.dev/api/events | wc -c

Fix:
- Migration `a1f0c0de0013` adds a nullable `events.cover_sha256` (`String(64)`), backfilled from the existing covers. Event create, patch and the split set it whenever the cover changes.
- New public route `GET /event-covers/{event_id}.jpg?v={first 12 characters of the sha}` decodes the stored data URI. It answers 404 for an inactive or leadership-only event, using the same predicate as the public query (78.9), so a leadership-only cover is never public. `Cache-Control: public, max-age=86400` and an `ETag`.
- Public rows carry `cover_url` (null without a cover) in place of `cover_image_data`. `events-public.js` reads `cover_url` where it now reads `cover_image_data` for the Discord preview (about line 776). Admin responses keep the data URI, so the admin form is unchanged.
- Caddy needs `reverse_proxy /event-covers/* 127.0.0.1:8000`. `/api/events` is already routed.

This is a prerequisite only for caching `/api/events` offline. It also makes every page load lighter, so it is recommended whether or not the app work proceeds. Without it, phase 1 does not cache the API (78.6).

### 78.6 Phase 1: installable app shell

**Routes** (public, GET, no auth):

| Route | Serves |
|---|---|
| `/manifest.webmanifest` | Generated per request from the Kingdom: `name`, `short_name`, `lang`, `start_url` `/events?source=pwa`, `scope` `/`, `display` `standalone`, `theme_color` from `kingdoms.color` when it is a valid hex (else the shipped color), `background_color`, icons 192 and 512 plus a maskable 512 |
| `/sw.js` | `static/sw.js` with `Cache-Control: no-cache, max-age=0` and `Content-Type: text/javascript`. The route replaces the token `__STATIC_ASSET_VERSION__` with `STATIC_ASSET_VERSION`, so the script's bytes change on every static bump, which is what makes browsers install the new worker and drop old caches |
| `/static/icons/*` | The icon files, under the existing static mount |

The worker is served from `/sw.js`, not `/static/sw.js`, because a worker controls only paths under its own directory unless the server adds `Service-Worker-Allowed`.

**Caddy and Cloudflare.** `/manifest.webmanifest` and `/sw.js` each need a `reverse_proxy` line and a `systemctl restart caddy` (README "Adding a new public route"); an unlisted path gets an empty 200 and the app never sees it. Cloudflare caches `.js` files by extension, and `/sw.js` is outside `/static`. Verify after deploy with `curl -sI https://ks138.taraka.dev/sw.js | grep -i cf-cache-status`, which must not say `HIT`. If it does, add a Cloudflare cache rule that bypasses `/sw.js`, because a stale worker means browsers never see the new version token.

**Pages.** `public_pages.render_public_page` adds `<link rel="manifest">`, `<meta name="theme-color">` and an `apple-touch-icon` link. A new classic script `static/pwa.js` (one IIFE, no globals, pure section exported under `__SAMAYA_TEST__`) registers the worker when `navigator.serviceWorker` exists and the URL has no `preview_theme`. It also holds the install prompt: on Chromium it keeps the `beforeinstallprompt` event for an "Install app" button; on iOS Safari it shows "Share, then Add to Home Screen" when the page is not already in standalone mode (`matchMedia('(display-mode: standalone)')` or `navigator.standalone`).

**Fetch handler.**

| Request | Strategy |
|---|---|
| Navigation to `/events`, `/events/{slug}`, `/feedback` | Network first with a 4 second timeout, then the cached copy, then `/static/offline.html` |
| `/api/events`, `/api/events/{slug}`, `/api/alliances`, `/api/kingdom-branding` | Network first, falling back to cache. After phase 0 only; before it the API is not cached. The page shows "Offline, last updated hh:mm" when it renders from cache |
| `/static/*`, `/theme/*.css`, `/theme-assets/*`, `/event-covers/*` | Stale while revalidate |
| `/admin*`, `/auth*`, `/invite*`, `/webhooks*`, `/health`, ICS feeds, any non-GET | No `respondWith`; the browser handles them |

Cache rules: never `put` a response that has `Cache-Control: no-store` (this covers the superadmin's `?preview_theme=` pages, `public_pages.mark_preview`), a request whose URL has `preview_theme`, or a non-200 status. Caches are named `samaya-static-{STATIC_ASSET_VERSION}` and `samaya-data-v1` (at most 30 entries); `activate` deletes any other cache.

A worker's `fetch(event.request)` carries cookies like any same-origin request, so the language and theme cookies work as usual while online. The cache lives on one device and network first overwrites the stored page on every successful load, so the offline copy is the last page that device saw, in the language it saw it. `ignoreVary` is used so that copy matches despite `Vary: Accept-Language, Cookie`. `offline.html` is plain English and sits outside the i18n pipeline (78.16).

Installed scope is `/`, so the admin console also opens inside the app window when a coordinator follows a link. This is acceptable and recorded as an open point (O7).

### 78.7 Data model (migration `a1f0c0de0014`)

Additive. The downgrade drops the three tables and the column, which loses subscriptions (players resubscribe). Models go in `models/db.py`, so `alembic check` sees them. Guarded creation as in the earlier revisions, verified on Postgres 16 with upgrade, `alembic check`, downgrade and re-upgrade. Every datetime read goes through `ensure_utc`. JSON columns are declared `JSON(none_as_null=True)`, so Python `None` is SQL NULL and the CHECKs below mean what they say.

**`push_subscriptions`**

| Column | Notes |
|---|---|
| `id` | primary key |
| `endpoint_hash` | `String(64)`, unique, sha256 hex of the endpoint; the public handle (not `CHAR`, which pads on Postgres) |
| `endpoint`, `p256dh`, `auth` | the browser's subscription; `auth` is also the management proof (78.11) and is stored as is because encryption needs it |
| `locale` | an enabled locale, for the service worker's `Intl` calls |
| `lead_mode` | `event_defaults` (default) or `custom` |
| `custom_offsets` | JSON list of minutes; CHECK `lead_mode = 'event_defaults' OR custom_offsets IS NOT NULL` |
| `kingdom_wide`, `announcements` | booleans, both default true |
| `created_at`, `prefs_updated_at`, `last_seen_at`, `last_success_at`, `last_failure_at`, `failure_count`, `disabled_at` | housekeeping. `prefs_updated_at` is set on every write to the preferences or rules |

**`push_rules`**: `id`, `subscription_id` (cascade), `kind` (`alliance`, `type`, `series`), `ref` (Text, not null: a tenant id, an event type id or a `series_id`), `label` (Text, the event name shown in the panel, at most 120 characters), `action` (`follow` or `mute`), `offsets` (JSON, nullable). Unique on (`subscription_id`, `kind`, `ref`). CHECKs: `alliance` is only `follow`, `type` is only `mute`, `offsets` only on `series`. `ref` carries no foreign key, so a deleted alliance or type leaves a harmless rule that goes away with the subscription. One text column instead of a nullable id and a nullable text keeps the unique constraint honest, since a UNIQUE over NULLs does not stop duplicates on Postgres.

**`push_sends`**: `id`, `subscription_id` (cascade), `series_id`, `occurrence_date`, `offset_minutes`, `occurrence_id` (nullable, no foreign key, informational only), `due_at_utc`, `status` (`sending`, `sent`, `failed`, `expired`), `detail`, `claimed_at_utc`. Unique on (`subscription_id`, `series_id`, `occurrence_date`, `offset_minutes`), which is the at-most-once guarantee. It does not use the occurrence id, so recreating an occurrence cannot repeat a push (decision 11). Index on (`status`, `claimed_at_utc`).

**`event_occurrences.changed_at`**: nullable, set by `PATCH /api/occurrences/{id}` (cancel, restore, move, message) and by generation when it changes a start time. Needed by the stale rule in 78.8, since a moved occurrence changes the occurrence row and not `events.updated_at`.

**Cascade.** The test database does not enable SQLite foreign keys (no `PRAGMA foreign_keys` in `conftest.py`), so the service deletes a subscription's rules and sends explicitly in code, and `ON DELETE CASCADE` is only the Postgres backstop. Tests cover both.

**Retention** (daily job, 03:10 UTC): delete `push_sends` older than 30 days; delete a subscription when the latest of `last_seen_at`, `last_success_at` and `created_at` is older than 120 days, or `disabled_at` is older than 7 days; delete `series` rules whose series has no occurrence today or later, so finished one-off events do not fill the cap.

**Caps** (enforced in the API): 5,000 subscriptions in total (it matches the in-memory evaluation in 78.9; a larger audience needs a different design, O4); per subscription 50 alliance rules, 100 type mutes, 200 series rules, 5 offsets in any one list, each offset 0 to 10,080 minutes. At the global cap the API first prunes subscriptions that never had a successful send and are over 7 days old, then answers 503. The count-then-insert check can overshoot by a few under concurrent requests, which is acceptable.

### 78.8 Who gets which push

For one public occurrence and one subscriber, in order:

0. The occurrence is not cancelled and the event is public and active (guaranteed by the public query).
1. A `series` rule with `mute` for the event's `series_id`: no push.
2. A `series` rule with `follow`: push, skipping steps 3 to 5.
3. A `type` rule with `mute` for the event's type: no push.
4. The event is an announcement (no duration) and `announcements` is false: no push.
5. A kingdom-wide event needs `kingdom_wide` true. An alliance event needs an `alliance` rule for any alliance in the event's audience. The combined view returns one row per audience alliance, so the occurrence matches if any of its rows does. This deployment has one Kingdom (`get_public_kingdom`), so "kingdom-wide" needs no Kingdom reference.

Precedence is series, then type, then alliance. New events and new event types match automatically when they fit the rules, with no re-subscribe. A series follow cannot reach a leadership-only or inactive event, because step 0 comes first.

**Lead times for a match**, first that applies: the series rule's `offsets`; else the subscriber's `custom_offsets` when `lead_mode` is `custom`; else the event's own reminder minutes. Offsets above 10,080 are ignored for push. An event with no reminders and no custom times sends nothing in the default mode.

**Not stale.** A (subscriber, occurrence, offset) is eligible only when its `due` time is later than the latest of the subscriber's `created_at` and `prefs_updated_at`, the event's `updated_at`, and the occurrence's `generated_at` and `changed_at`. Without this, a player who subscribes at 19:10 would receive the 19:00 push, adding a new lead time would fire the ones already past, and moving or creating an event would fire "30 minutes until" with 20 minutes left. The Discord engine has the same guard (`_new_delivery_state` cancels a reminder whose time had already passed when it was generated).

One push per (subscriber, series, date, offset), however many alliances or rules matched.

| Subscriber | Event | Result |
|---|---|---|
| Follows MOD, defaults | Bear Hunt, MOD, reminders 60 and 0 | Pushes at 60 minutes and at the start |
| Follows MOD, custom `[45, 5]` | Same | Pushes at 45 and 5 minutes |
| Follows MOD, mutes type Arena | Arena, MOD | Nothing |
| Follows MOD, follows series Trap | Trap, owned by another alliance | Pushes (step 2) |
| Follows nothing, `kingdom_wide` true | Kingdom-wide KvK | Pushes at the event's reminder minutes |
| Follows MOD, mutes series Bear Hunt, follows type Bear Hunt | Bear Hunt, MOD | Nothing (series mute wins) |
| Any | Leadership-only or cancelled occurrence | Never |
| Subscribes at 19:10 | 20:00 event with a 60 minute offset | Not pushed (due 19:00 is before the subscription) |
| Any | A 20:00 event moved to 19:20 at 19:05 | The 30 minute push is not sent (due 18:50 is before the move); a 5 minute push at 19:15 is |

### 78.9 Engine

New `services/push_engine.py` with an injectable async sender (`get_pusher()`, like `get_discord()`), so tests use a `FakePush`. The sender interface is `async send(subscription, payload, ttl, urgency, topic) -> PushResult`. The production sender is decision 14.

**Public query.** The rule "leadership-only and inactive events appear nowhere public" stays in one place. `services/public_events.py` factors its filter into one shared predicate and adds `public_occurrences_for_push(db, first, last)`, which applies that predicate and `status != 'cancelled'` and returns occurrences with their events and audience alliances, with `Event.cover_image_data` deferred. It skips what the push engine does not need (`Delivery` rows, destinations, the full tenant map). `public_rows` is not called every minute: it runs about six queries and loads every event whole, including the cover.

**Jobs.** `register_push_jobs(scheduler)` adds `push_tick_job` (every minute, `max_instances=1`, `coalesce=True`, `misfire_grace_time=30`) and the daily prune job, only when `push_enabled()` is true (read at call time from the environment, so tests can toggle it without running the lifespan).

`run_push_tick(session_factory, pusher, now=None)`:

1. **Exit early** when there are no enabled subscribers.
2. **Offsets in use.** The union of every enabled subscriber's offsets and every event's reminder minutes, computed once.
3. **Candidates.** Occurrences for UTC dates `today - 1` to `today + 7`, kept when `effective_start` is within `[now - AT_START_GRACE, now + 10,080 minutes]` and some offset in use puts a due time in `(now - 15 minutes, now]`. The date window keys on `occurrence_date`, so an occurrence moved by more than a day from its original date can fall outside it, the same limit the public page has.
4. **Matching and eligibility.** For each candidate, each enabled subscriber is evaluated with 78.8, including the not-stale rule. Subscribers and rules are loaded into memory once per tick. A pair is due when `due = start - offset` satisfies `now - 15 minutes < due <= now` and the reminder has not expired under the Discord engine's rule (`_reminder_expired`, made public: an at-the-start push has the 5 minute grace and every other offset is dropped once the event has started). A pair whose due time is older than 15 minutes is counted as `late` and logged at WARNING.
5. **Claim.** Insert the `push_sends` row as `sending` and commit before any network call. A unique violation means the pair was already claimed and it is skipped. The insert is done as try, catch `IntegrityError`, so it runs on SQLite in tests and on Postgres. One commit per claim; the claim cost is measured in phase 2 against a target of 500 claims in under 2 seconds on `lxc-taraka`.
6. **Send.** At most 500 sends per tick, ordered by due time, with 20 in flight, a 5 second timeout each and a 45 second deadline for the whole tick. At the deadline no new send starts and the unstarted pairs are counted as `deferred`; they are picked up by the next tick while still inside their 15 minute window. The sender never blocks the event loop.
7. **Record.** `sent`, `failed` or `expired`, then update the subscriber.

`TTL` is the seconds until the push would be skipped as late (15 minutes after its due time, 5 for an at-the-start push), at least 60 and at most 900. `Urgency` is `high` at 15 minutes or less and `normal` otherwise. `Topic` is the first 32 characters of the base64url sha256 of `series:date:offset`, so a duplicate replaces rather than stacks. `VAPID_SUBJECT` supplies the subject.

**Responses.**

| Push service answer | Result |
|---|---|
| 201 | `sent`, `last_success_at` set, `failure_count` reset to 0 |
| 404 or 410 | `expired`, the subscription and its rules and sends are deleted |
| 429 | `failed`, no retry (at most once), no penalty to the subscriber |
| Other 4xx (including FCM's 403 after a key change), 5xx, timeout, network error | `failed`, `failure_count` plus 1, `last_failure_at` set |

A subscriber is disabled (`disabled_at` set) when `failure_count` is 10 or more and `last_success_at` is null or older than 3 days, so a push-service outage cannot disable healthy subscribers. A `sending` row older than 10 minutes becomes `failed` with "interrupted; it may or may not have been delivered", as for Discord. If the process dies between the claim and the send, that push is lost and never repeated.

Cancelled, moved and deactivated occurrences behave correctly because eligibility is recomputed from the current start every tick. A push already sent for an offset is not repeated when the event is later moved, which matches a posted Discord delivery (§66.4a).

Cost: one query for subscribers and rules, one push query over nine UTC dates, and at most 500 outbound requests per tick. At 5,000 subscribers and a handful of candidate occurrences per tick this is well inside a minute.

### 78.10 Payload and notification

Plaintext JSON, version 1, at most 1,536 bytes (the encrypted body must stay under 4,096):

    {"v":1,"occurrence_id":123,"series":"<32 hex>","name":"Bear Hunt","minutes":30,
     "start":"2026-10-04T20:00:00+00:00","url":"/events?occ=123","title":"Bear Hunt","body":"30 minutes until Bear Hunt"}

`name` and `title` are cut at 120 characters. `title` and `body` are the English fallback. The service worker builds the shown text itself. Title is the event name. For the body it computes `remaining = round((start - now) / 60 s)` and uses `minutes` when `abs(remaining - minutes) <= 2` and `remaining` otherwise, so a late delivery does not claim the wrong time; a result of 0 or less, or `minutes` of 0, reads as "now". The largest whole unit is used (days, then hours, then minutes, as §77.4) with `numeric: 'always'`, and "now" is `format(0, 'second')` with `numeric: 'auto'` (78.3). If `Intl` throws for the locale the English fallback is shown.

The notification always shows (78.3). It uses `tag` `occ-{id}` with `renotify: true`, so the 5 minute push replaces the 60 minute one for the same occurrence and still alerts; this is a deliberate choice. It has a 192 px icon and a monochrome badge and no action buttons (iOS ignores them).

**Click.** `notificationclick` focuses an open window of the app or opens one, and navigates only to a same-origin path from the allowlist (`/events`, `/events/*`, `/feedback`). Anything else opens `/events`. `?occ=` is a hint: when the page has that occurrence it scrolls to it and highlights it, and otherwise ignores it.

**Keeping a subscription alive.** A browser can replace its subscription (expiry, a key change). The worker does not rely on `pushsubscriptionchange`, which Safari does not fire reliably and which has no storage to read the old credentials from. Instead the page reconciles on every load: it keeps the subscription's `hash` and `auth` in `localStorage`, and when `getSubscription()` yields a different endpoint, or the key from `/api/push/config` differs from the subscription's `applicationServerKey`, it reads the old preferences with the old credentials, registers the new subscription with them and deletes the old one. If the old one is gone the person starts again from defaults. A subscriber who never opens the page after a browser-side change is lost until they do; the `last_success_at` age shows it in the health card.

### 78.11 API (public, JSON, no cookies)

All under `/api/push`. `/api/*` is already routed by Caddy, so there is no new Caddy line. Management routes put the subscription's `hash` in the path and its `auth` secret in `X-Push-Auth`, compared with `hmac.compare_digest`. An unknown hash and a wrong secret both answer 404, and a miss compares against a dummy value so timing does not tell them apart. Every management response carries `Cache-Control: no-store`.

| Route | Purpose |
|---|---|
| `GET /config` | `{enabled, public_key}`. `Cache-Control: max-age=300` |
| `POST /subscriptions` | Body: the browser's `PushSubscription` JSON, `locale`, and the preferences. Validates the endpoint (78.13). 201 on create. If the hash already exists, 200 and an update only when the body's `auth` equals the stored one; otherwise 409 and no change. A different `auth` for a known endpoint cannot overwrite it |
| `GET /subscriptions/{hash}` | The stored preferences and rules; updates `last_seen_at` at most once a day |
| `PUT /subscriptions/{hash}` | Replaces the preferences and rules in one call (the panel sends the whole state), validated against the caps in 78.7 |
| `PATCH /subscriptions/{hash}/series` | Adds, changes or clears one series rule (the per-row bell) |
| `POST /subscriptions/{hash}/test` | Sends a test push. 3 per subscription per hour |
| `DELETE /subscriptions/{hash}` | Deletes the subscription and everything under it (the erasure path) |

**Rate limits** use `services/rate_limit.RateLimiter`, one instance per route: 10 subscribes per client per hour and 60 in total per hour, and a lighter limit on the other routes. They depend on the client IP being right. Today `_client_ip` trusts the leftmost `X-Forwarded-For`. Behind Cloudflare that value can be set by the client, which bypasses any per-IP limit; if Caddy instead replaces the header with the tunnel's address, every visitor shares one bucket. Which of these applies is unknown. Before phase 2: run `grep -n 'trusted_proxies\|X-Forwarded-For\|header_up' /etc/caddy/Caddyfile` on `lxc-taraka`, and key the limiter on `Cf-Connecting-Ip` (which Cloudflare sets and the README says is the only path to the app), with the forwarded header as a fallback and a test that a client-sent header does not change the key. The limiter also gets eviction of empty histories, since it never frees keys today. This fix also covers the existing ticket board limiter, which has the same exposure (O9).

Superadmin: `GET /admin/api/push/health` returns subscriber counts, sends in the last 24 hours by status and the `late` and `deferred` counts, the time of the last tick and the oldest failed send, and the Delivery log tab shows it as a card. Admin responses never include endpoints or keys.

### 78.12 Public page UI

A new classic script `static/push.js` (one IIFE, no globals, pure section exported under `__SAMAYA_TEST__`), loaded on both pages. Strings live in `app/i18n/en.json` under `public.push.*` and in the seven machine-drafted catalogues. The i18n work includes the page prefixes in `render_public_page`, the file list in `test_i18n.py` (`PAGES`, which now covers only `events-public.js` and `feedback.js`; otherwise `test_no_unused_keys` fails), and the markup fallbacks. The ESLint config (`eslint.config.mjs`) uses an explicit globals list that lacks `navigator`, `Notification`, `self`, `caches` and `clients`, so those are added for `pwa.js`, `push.js` and `sw.js`.

- **Header button** "Notifications", hidden when the browser lacks support or the server says `enabled: false`. On iOS Safari outside the Home Screen it shows the install steps instead of a subscribe control.
- **Panel** (a `.modal` with Escape and focus return, as in `feedback.js`):
  - Status and a subscribe or unsubscribe control. The permission prompt appears only from this tap.
  - *What*: a checkbox per alliance (the current alliance is pre-ticked on `/events/{slug}`), "Kingdom-wide events", "Announcements", and a checkbox per event type (all ticked; unticking mutes the type).
  - *When*: "Use each event's reminders" or "My own times", with chips (at the start, 5 minutes, 15 minutes, 30 minutes, 1 hour, 3 hours, 1 day) and a custom entry, at most 5.
  - *Followed and muted events*: the list of series rules with their `label`, each clearable.
  - A "Send me a test notification" button and a privacy note (78.13) that names the browser vendors' push services.
- **Per-row bell** on schedule and list rows: a toggle with `aria-pressed` and a small menu (Follow, Mute, Use my defaults). It acts on the row's `series_id`, so the public row payload gains an opaque `series_id`.
- **Permission denied** shows how to re-enable it in the browser's site settings, since the page cannot ask again.
- **State** is one object; the DOM reacts to it and never reads state from the DOM. Saves are optimistic, debounced 500 ms, and roll back with a toast on failure.
- **Accessibility and style:** WCAG 2.0 AA contrast checked for every new token pair, visible focus, full keyboard use, labels on every input, BEM classes, tokens from the page's `:root`, no inline styles or handlers, `prefers-reduced-motion` respected. `STATIC_ASSET_VERSION` is bumped with each static change.

### 78.13 Security, abuse and privacy

- **Server-side request forgery.** The endpoint comes from an anonymous visitor and the server will call it. Parse it once, with one parser. Reject any backslash, `@`, whitespace or non-ASCII character. Lowercase the host and strip a trailing dot. Accept only `https`, port 443 (an explicit `:443` is accepted, any other port is not), a hostname (never an IP literal) on the allowlist, at most 2,048 characters. Rebuild the URL from the validated parts and send to exactly that string, so a parser difference between validator and sender cannot matter. A suffix rule matches one or more labels under the suffix, never the suffix alone. The sender sets `trust_env=False` (no proxy variables) and `follow_redirects=False`, with a 5 second timeout, and discards the response body. Starting list: `fcm.googleapis.com`, `updates.push.services.mozilla.com`, `web.push.apple.com` and `*.push.apple.com`; Edge's host (`*.notify.windows.com`) is added only if a real Edge subscription needs it. `PUSH_ENDPOINT_HOSTS` overrides the list. It is checked against real Chrome, Firefox, Safari and Edge subscriptions before release (O6).
- **Key shapes.** `p256dh` decodes to 65 bytes and `auth` to 16, both base64url. Anything else is a 422.
- **Spam.** The limits in 78.11, the global cap and the per-subscription caps in 78.7. The limiter is in memory, valid because there is one worker.
- **Tampering.** Changing a subscription needs its `auth` secret, which only that browser and this server know, and a known endpoint cannot be overwritten without it (78.11). There is no cookie auth, so no CSRF surface; requests must be `application/json`. No CORS headers are sent. The secret is stored as is because encryption needs it, so a database or backup leak lets someone change the preferences of those subscriptions. It does not let them send a push, which needs the VAPID private key.
- **Content.** Notifications are plain text, never HTML. Event names come from coordinators and are truncated.
- **Service worker.** It is served from a fixed same-origin route, does not touch the paths listed in 78.6, never stores a `no-store` or preview response, and navigates only to allowlisted same-origin paths on click.
- **Privacy.** The application database stores the endpoint and keys, locale, preferences and timestamps, and no IP address or user agent. The rate limiter keeps IPs in memory only. Server, Caddy and Cloudflare logs outside the app can hold IP and user agent, and the endpoint's host reveals the browser family. The app's logs never contain an endpoint or key; the access log does contain the subscription hash in the URL, which on its own grants nothing without the `auth` secret. An endpoint is a stable pseudonymous identifier, so treat it as personal information under GDPR, PIPEDA and Quebec Law 25: the panel's privacy note says what is stored and that the browser's push service (Google, Apple or Mozilla) delivers the notification, `DELETE` is the erasure path, and idle subscriptions are pruned after 120 days.
- **VAPID private key** only in `.env`, never in git, logs or any API response.

### 78.14 Operations

- **Environment** (`.env.example` gains these): `SAMAYA_PUSH_ENABLED`, `VAPID_PRIVATE_KEY`, `VAPID_PUBLIC_KEY`, `VAPID_SUBJECT` (a `mailto:` address, set by the owner), optional `PUSH_ENDPOINT_HOSTS`. The key generation command is documented in the README when this is built.
- **Key rotation breaks every subscription**, because a browser binds a subscription to the public key it was made with, and FCM answers such sends with 403 rather than 404 or 410. The page's reconciliation (78.10) resubscribes on load. Do not rotate casually.
- **Kill switch:** set `SAMAYA_PUSH_ENABLED=0` and restart. The bell disappears, the jobs are not registered, and subscriptions are kept. Pause the Uptime Kuma monitor first.
- **Backup:** the new tables are inside the nightly `ops/backup.sh` dump. The VAPID keys live in `.env`; whether the restic backup of `/opt/taraka` includes `.env` must be checked (O8). Restore check: `SELECT count(*) FROM push_subscriptions`, then a test push.
- **Monitoring:** an Uptime Kuma keyword monitor on `https://ks138.taraka.dev/api/push/config` expecting `"enabled":true`, plus the admin health card.
- **Deploy order:** migrations, then the Caddy lines for the phase being shipped, then `systemctl restart caddy`, then the app. RISK: a Caddy restart drops connections for a moment, and an unlisted path answers an empty 200 rather than failing, so check each new path through the public hostname. Roll back by removing the lines and restarting again.
- **Rollback:** code-only rollback leaves the tables unused. `alembic downgrade a1f0c0de0012` drops everything from §78 (both revisions) and deletes all subscriptions; `events.cover_sha256` goes with phase 0.

### 78.15 Tests and verification

- `tests/test_pwa_routes.py`: manifest content and Kingdom colour handling, `/sw.js` content type, no-cache header and version substitution, and a table test of the worker's routing (which paths get `respondWith`).
- `tests/test_event_covers.py`: `cover_url` in public rows, no data URI anywhere public, 404 for leadership-only and inactive events, `cover_sha256` kept in step on create, patch and split, ETag.
- `tests/test_push_subscriptions_api.py`: create, 200 and 409 on an existing hash, validation (endpoint host, key lengths), every cap and the cap pruning, management proof (wrong secret and unknown hash both 404), rate limits, delete, test push limit, `no-store` headers.
- `tests/test_push_rules.py`: the 78.8 matrix including every row of the table, precedence, announcements, kingdom-wide, leadership-only and cancelled never, offsets precedence, offsets above 10,080, and the not-stale rule (new subscriber, new offset, event edit, occurrence move).
- `tests/test_push_engine.py` with `FakePush` and an injected clock: due window, 15 minute late limit and the `late` count, at-the-start grace, at-most-once across two ticks and a simulated crash, no repeat after the occurrence is deleted and recreated (split, deactivate then reactivate), cancel, move and deactivate, one push for several matching alliances, the 500 per tick cap and the 45 second deadline with `deferred`, response handling (201, 404 and 410 delete, 429, 5xx counting and the disable rule), stale `sending` recovery, retention including finished one-off series rules, explicit child deletion with SQLite foreign keys off, and a slow `FakePush` that must not block another coroutine.
- `tests/test_push_security.py`: endpoint allowlist cases (IP literal, userinfo, backslash, trailing dot, uppercase, port, lookalike suffix, `http`, non-ASCII), payload size under 1,536 bytes for a 120 character name with multibyte text, no endpoint or key in any app log line or admin response, nothing but `series_id` added to the public payload, a client-sent forwarded header not changing the limiter key.
- `tests/test_push_jobs.py`: `register_push_jobs` adds nothing when disabled and both jobs when enabled.
- `tests/test_migration_0013.py` and `test_migration_0014.py`: upgrade, `alembic check`, downgrade, re-upgrade on Postgres 16 (opt-in, like the 0004 and 0005 tests), plus the backfill of `cover_sha256`.
- vitest: `push.js` pure functions (state derivation, preference diffing, offset validation, page-load reconciliation), `sw.js` notification builder over all eight locales including "now" at the start, a late delivery and the English fallback, and the fetch-handler routing table.
- **Device matrix, by hand before release:** Android Chrome in a tab and installed, iOS 16.4 or later from the Home Screen, desktop Chrome, desktop Firefox, desktop Safari. For each: subscribe, test push, a scheduled push five minutes ahead, click opens the right page, unsubscribe, permission denied path, and an offline load. Also record each browser's real endpoint host for the allowlist.
- **After each deploy:** `curl -s -o /dev/null -w '%{http_code} %{size_download}B %{content_type}\n' https://ks138.taraka.dev/manifest.webmanifest` (a size of 0B means the Caddy line is missing), the same for `/sw.js` and `/event-covers/<id>.jpg?v=...`, `curl -sI https://ks138.taraka.dev/sw.js | grep -i cf-cache-status`, and `curl -s https://ks138.taraka.dev/api/push/config`.

### 78.16 Not built

Quiet hours (the OS does it), accounts and cross-device sync, email and SMS, Declarative Web Push (a later option once iOS 18.4 and later is the norm), `pushsubscriptionchange` handling and any worker-side storage (the page reconciles instead), notification action buttons, badge counts, a digest, linking a Discord identity, notifications for coordinators or about feedback tickets, a localised offline page, and a server-side message catalogue for push text.

### 78.17 Open questions for the owner

| # | Question | Why it matters |
|---|---|---|
| O1 | Which icon artwork, and may a crest be used? | Phase 1 needs 192, 512 and maskable PNGs. No art is generated or copied |
| O2 | Do phase 0 (payload diet) first? | Needed for offline `/api/events`, recommended regardless. It adds a migration and a route |
| O3 | Which address goes in `VAPID_SUBJECT`? | Push services use it to contact the sender. It stays in `.env`, not in git |
| O4 | How many subscribers do you expect? | The design is sized for 5,000 with in-memory evaluation. Beyond that it needs a due-time index |
| O5 | Are `http-ece` and `py-vapid` maintained and compatible with the pinned `cryptography`? | Decision 14 depends on it. If not, write a small sender against the RFC 8291 vectors |
| O6 | Is the host allowlist complete? | Checked on real devices before release, since a wrong list silently blocks a browser |
| O7 | Should installed-app scope exclude `/admin`? | Scope `/` lets admin links open in the app window; a narrower scope would send `/feedback` to a browser tab instead |
| O8 | Does the restic backup include `/opt/taraka/.env`? | Losing the VAPID private key invalidates every subscription |
| O9 | What does Caddy do with `X-Forwarded-For`, and is `Cf-Connecting-Ip` passed through? | The per-IP limits (this feature's and the ticket board's) are only as good as the client IP. Run the `grep` in 78.11 |

### 78.18 Review record

The first draft was reviewed 2026-10-04 by two independent reviewers with no stake in it, one on security and web push protocol, one on fit with the code and the engine. Each finding was checked against the code or a source before it was accepted. Items marked "corrected" were wrong or overstated in the review.

| Finding | Outcome |
|---|---|
| Occurrence rows are deleted and recreated, which would cascade away `push_sends` and repeat a push | Confirmed in `_retire_occurrence`. Dedupe key is now `series_id`, date and offset; no occurrence foreign key (decision 11, 78.7) |
| A synchronous sender (`pywebpush`) blocks the single event loop; 1,000 slow sends outlast the minute and tick coalescing | Confirmed. Async sender, 5 second timeout, 45 second deadline, 500 per tick, `misfire_grace_time` (decision 14, 78.9) |
| A new subscriber, a new lead time or a moved event fires stale pushes; the spec wrongly said Discord behaves the same | Confirmed against `_new_delivery_state`. Not-stale rule and `event_occurrences.changed_at` (78.8) |
| `public_rows` every minute loads every event with its cover and runs about six queries | Confirmed. A push-specific query sharing one predicate (78.9) |
| `format(0, 'minute')` is "this minute", not "now" | Confirmed by running Node on eight locales. Use `format(0, 'second')` (78.10) |
| Per-IP rate limiting is bypassable or collapses to one bucket; the limiter never evicts | Plausible, unverified here. Fix, check and eviction specified (78.11, O9). Also affects the ticket board |
| Phase 0 needs a column and migration, and `events-public.js` reads the cover | Confirmed. `events.cover_sha256`, `a1f0c0de0013`, route and JS change (78.5) |
| `/api/push/*` needs no Caddy line; phase 1 needs two lines, not three | Confirmed against the README route list (78.4, 78.6) |
| `/sw.js` may be edge cached by Cloudflare | Plausible. `cf-cache-status` check and bypass rule (78.6) |
| Upsert on POST lets anyone with an endpoint overwrite its keys | Accepted. 200 only with the matching `auth`, else 409 (78.11). The 409 reveals existence only to someone who already holds the endpoint, which is itself the secret |
| The 8 character hash claim for logs is false because the path carries the whole hash | Accepted as a wording fix (78.13). The hash is a lookup handle that grants nothing without `auth`, so it stays in the path |
| "No IP stored" overstated; GDPR, PIPEDA and Law 25 | Accepted. Wording, push-service recipients in the notice, `DELETE` as erasure (78.13) |
| SSRF parser differentials, trailing dot, wildcard width, proxy variables | Accepted (78.13) |
| Service worker caches `no-store` and preview pages; "cookies invisible to the worker" is false | Accepted. Cache rules and wording fixed (78.6). Corrected: the reviewer said `ignoreVary` serves the first cached language to everyone; the cache is per device and network first overwrites it on each load, so it holds the last page that device saw |
| TTL up to an hour delivers an old "60 minutes" push at the start | Accepted. TTL tied to the late limit; wording computed from the time left (78.9, 78.10) |
| `pushsubscriptionchange` has no storage and Safari does not fire it | Accepted. Dropped for page-load reconciliation (78.10, 78.16) |
| JSON NULL versus CHECK; `CHAR(64)`; dead series rules fill the cap; idle receivers pruned at 120 days; datetime handling | Accepted (78.7) |
| Cap of 20,000 contradicts in-memory evaluation sized for thousands | Accepted. Cap 5,000 (78.7) |
| SQLite tests do not enforce foreign key cascades | Confirmed. Explicit deletion in code, both tested (78.7, 78.15) |
| ESLint globals, `test_i18n.py` `PAGES`, and the lifespan not being run in tests | Accepted (78.12, 78.9 `register_push_jobs`) |
| The auth secret is stored in cleartext; a Uptime Kuma monitor alarms on the kill switch; the single Kingdom assumption; Topic plus tag replaces the earlier notification | Accepted as documented (78.13, 78.14, 78.8, 78.10) |

Not verified in review or here: Safari's behaviour on a push without a visible notification, the real push hostnames of Edge and Samsung Internet, the maintenance state of `http-ece` and `py-vapid`, the Topic length rule against the RFC text, and Cloudflare's and Caddy's actual handling of the forwarded headers.
